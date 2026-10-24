"""The guarded deploy webhook: authentication per provider, branch filter, locks, secrets."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.cli import cli
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, PipelineRun, Setting
from orchestrator.db.session import get_engine
from orchestrator.deploy.watch import forget_all
from orchestrator.domain.enums import HealthState, PipelineKind
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.health.engine import HealthEngine
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

REGISTRY = """\
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: portainer-vm
    name: Portainer VM
    node: pve1
    guest_kind: vm
    guest_id: 101
  - slug: dev-xymbols
    name: dev.xymbols
    parent: portainer-vm
    controller: {type: portainer, endpoint: 1, stack: dev-xymbols}
    deploy:
      trigger: {webhook: {provider: PROVIDER, branch: dev}}
      guard:
        pre: {probes: true}
        post: {probes: true, timeout_s: 1, settle_s: 0}
  - slug: media
    name: media
    parent: portainer-vm
    controller: {type: portainer, endpoint: 1, stack: media}
"""

HEALTH: dict[str, HealthState] = {}


@pytest.fixture(autouse=True)
def fake_health(monkeypatch: pytest.MonkeyPatch) -> None:
    HEALTH.clear()
    forget_all()

    async def scan(self: Any, session: Any, service: Any, **kw: Any) -> ServiceVerdict:
        state = HEALTH.get(service.slug, HealthState.HEALTHY)
        return ServiceVerdict(
            state=state, reason=f"{service.slug} is {state.value}", probe_results=[]
        )

    monkeypatch.setattr(HealthEngine, "scan", scan)


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str) -> TestClient:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY.replace("PROVIDER", provider))
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    monkeypatch.setenv("ORCHESTRATOR_PUBLIC_URL", "https://orch.example.lan")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    return TestClient(create_app())


def new_secret(client: TestClient) -> str:
    r = client.post("/api/services/dev-xymbols/deploy/webhook")
    assert r.status_code == 200, r.text
    return str(r.json()["secret"])


def wait_run(client: TestClient, run_id: int) -> dict[str, Any]:
    for _ in range(200):
        body = client.get(f"/api/deploys/{run_id}").json()
        if body["status"] != "running":
            return body  # type: ignore[no-any-return]
        time.sleep(0.02)
    raise AssertionError("did not finish")


@pytest.fixture
def generic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    with make_client(tmp_path, monkeypatch, "generic") as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


@pytest.fixture
def gitlab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    with make_client(tmp_path, monkeypatch, "gitlab") as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


@pytest.fixture
def github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    with make_client(tmp_path, monkeypatch, "github") as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


# -- the secret ---------------------------------------------------------------------------------


def test_the_secret_is_shown_once_and_never_again(generic: TestClient) -> None:
    before = generic.get("/api/services/dev-xymbols/deploy").json()["webhook"]
    assert before["configured"] and before["secret_set"] is False
    secret = new_secret(generic)
    summary = generic.get("/api/services/dev-xymbols/deploy")
    assert secret not in summary.text and summary.json()["webhook"]["secret_set"] is True
    assert summary.json()["webhook"]["url"] == "https://orch.example.lan/hooks/deploy/dev-xymbols"
    # it never reaches the registry file either
    with Session(get_engine()) as s:
        audit = [
            a for a in s.exec(select(AuditEntry)).all() if a.action.startswith("deploy.webhook")
        ]
        assert audit and all(secret not in json.dumps(a.details) for a in audit)
    assert new_secret(generic) != secret  # rotate


def test_no_secret_for_a_service_without_a_webhook(generic: TestClient) -> None:
    assert generic.post("/api/services/media/deploy/webhook").status_code == 409
    assert generic.post("/api/services/ghost/deploy/webhook").status_code == 404


def test_removing_the_secret_stops_the_webhook(generic: TestClient) -> None:
    secret = new_secret(generic)
    assert generic.delete("/api/services/dev-xymbols/deploy/webhook").json()["removed"] is True
    r = generic.post("/hooks/deploy/dev-xymbols", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 401
    with Session(get_engine()) as s:
        assert s.get(Setting, "deploy.webhook.dev-xymbols") is None


# -- generic ------------------------------------------------------------------------------------


def test_generic_bearer_token_starts_a_guarded_deploy(generic: TestClient) -> None:
    secret = new_secret(generic)
    r = generic.post(
        "/hooks/deploy/dev-xymbols",
        headers={"Authorization": f"Bearer {secret}"},
        json={"ref": "refs/heads/dev", "sha": "abcdef0123456789", "pusher": "storm"},
    )
    assert r.status_code == 202, r.text
    done = wait_run(generic, r.json()["run_id"])
    assert done["status"] == "succeeded"
    trigger = next(s for s in done["steps"] if s["name"] == "trigger")["detail"]
    assert trigger["source"] == "webhook" and trigger["commit"] == "abcdef012345"
    assert trigger["pusher"] == "storm" and secret not in json.dumps(done)
    assert "guard_probes" in [s["name"] for s in done["steps"]]  # guarded, always


@pytest.mark.parametrize("how", ["header", "query"])
def test_generic_accepts_the_token_other_ways(generic: TestClient, how: str) -> None:
    secret = new_secret(generic)
    if how == "header":
        r = generic.post("/hooks/deploy/dev-xymbols", headers={"X-Deploy-Token": secret})
    else:
        r = generic.post(f"/hooks/deploy/dev-xymbols?token={secret}")
    assert r.status_code == 202, r.text
    wait_run(generic, r.json()["run_id"])


def test_a_wrong_or_missing_token_is_refused_and_starts_nothing(generic: TestClient) -> None:
    new_secret(generic)
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
        assert generic.post("/hooks/deploy/dev-xymbols", headers=headers).status_code == 401
    with Session(get_engine()) as s:
        assert (
            s.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.DEPLOY)).all() == []
        )


def test_unknown_or_webhookless_services_are_just_not_found(generic: TestClient) -> None:
    assert generic.post("/hooks/deploy/ghost").status_code == 404
    assert generic.post("/hooks/deploy/media").status_code == 404


def test_the_branch_filter_ignores_other_branches(generic: TestClient) -> None:
    secret = new_secret(generic)
    r = generic.post(
        "/hooks/deploy/dev-xymbols",
        headers={"Authorization": f"Bearer {secret}"},
        json={"ref": "refs/heads/feature-x"},
    )
    assert r.status_code == 202 and r.json()["started"] is False
    assert "not the branch" in r.json()["ignored"]
    with Session(get_engine()) as s:
        assert (
            s.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.DEPLOY)).all() == []
        )


# -- the locks and guards apply to a webhook as to anything else ------------------------------------


def test_a_switched_off_service_answers_423(generic: TestClient) -> None:
    secret = new_secret(generic)
    assert (
        generic.post("/api/services/dev-xymbols/switch", json={"state": "off"}).status_code == 200
    )
    r = generic.post("/hooks/deploy/dev-xymbols", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 423
    with Session(get_engine()) as s:
        refused = [
            a for a in s.exec(select(AuditEntry)).all() if a.action == "deploy.webhook_refused"
        ]
    assert len(refused) == 1 and refused[0].details["code"] == "off"


def test_a_failing_guard_means_the_webhook_deploy_is_skipped(generic: TestClient) -> None:
    secret = new_secret(generic)
    HEALTH["dev-xymbols"] = HealthState.FAILED
    r = generic.post("/hooks/deploy/dev-xymbols", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 202
    done = wait_run(generic, r.json()["run_id"])
    assert done["status"] == "skipped" and "before the deploy" in done["error"]
    portainer = generic.app.state.adapters.portainer  # type: ignore[attr-defined]
    assert portainer.calls == []  # Portainer was never asked


# -- GitLab -------------------------------------------------------------------------------------


def test_gitlab_token_header_and_push_event(gitlab: TestClient) -> None:
    secret = new_secret(gitlab)
    r = gitlab.post(
        "/hooks/deploy/dev-xymbols",
        headers={"X-Gitlab-Token": secret, "X-Gitlab-Event": "Push Hook"},
        json={
            "ref": "refs/heads/dev",
            "checkout_sha": "1234567890abcdef",
            "user_username": "storm",
        },
    )
    assert r.status_code == 202, r.text
    done = wait_run(gitlab, r.json()["run_id"])
    trigger = next(s for s in done["steps"] if s["name"] == "trigger")["detail"]
    assert trigger["provider"] == "gitlab" and trigger["commit"] == "1234567890ab"
    bad = gitlab.post("/hooks/deploy/dev-xymbols", headers={"X-Gitlab-Token": "wrong"})
    assert bad.status_code == 401


def test_gitlab_other_events_are_acknowledged_not_deployed(gitlab: TestClient) -> None:
    secret = new_secret(gitlab)
    r = gitlab.post(
        "/hooks/deploy/dev-xymbols",
        headers={"X-Gitlab-Token": secret, "X-Gitlab-Event": "Pipeline Hook"},
        json={},
    )
    assert r.status_code == 202 and r.json()["started"] is False


# -- GitHub -------------------------------------------------------------------------------------


def _signed(secret: str, body: bytes) -> dict[str, str]:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={sig}", "Content-Type": "application/json"}


def test_github_signature_is_verified_over_the_exact_body(github: TestClient) -> None:
    secret = new_secret(github)
    body = json.dumps({"ref": "refs/heads/dev", "after": "fedcba9876543210"}).encode()
    ok = github.post(
        "/hooks/deploy/dev-xymbols",
        content=body,
        headers={**_signed(secret, body), "X-GitHub-Event": "push"},
    )
    assert ok.status_code == 202, ok.text
    wait_run(github, ok.json()["run_id"])
    tampered = github.post(
        "/hooks/deploy/dev-xymbols",
        content=body + b" ",
        headers={**_signed(secret, body), "X-GitHub-Event": "push"},
    )
    assert tampered.status_code == 401
    assert github.post("/hooks/deploy/dev-xymbols", content=body).status_code == 401


def test_github_ping_is_answered(github: TestClient) -> None:
    secret = new_secret(github)
    body = b"{}"
    r = github.post(
        "/hooks/deploy/dev-xymbols",
        content=body,
        headers={**_signed(secret, body), "X-GitHub-Event": "ping"},
    )
    assert r.status_code == 202 and r.json()["ignored"] == "pong"


# -- CLI ------------------------------------------------------------------------------------------


def test_cli_webhook_shows_the_secret_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with make_client(tmp_path, monkeypatch, "gitlab"):
        run = CliRunner()
        first = run.invoke(cli, ["deploy", "webhook", "dev-xymbols"])
        assert first.exit_code == 0, first.output
        assert "Secret (copy it now" in first.output
        assert "https://orch.example.lan/hooks/deploy/dev-xymbols" in first.output
        again = run.invoke(cli, ["deploy", "webhook", "dev-xymbols"])
        assert "A secret is set" in again.output and "Secret (copy it now" not in again.output
        assert (
            "Secret (copy it now"
            in run.invoke(cli, ["deploy", "webhook", "dev-xymbols", "--rotate"]).output
        )
        assert "no webhook" in run.invoke(cli, ["deploy", "webhook", "media"]).output
        assert (
            "Secret removed"
            in run.invoke(cli, ["deploy", "webhook", "dev-xymbols", "--remove"]).output
        )
