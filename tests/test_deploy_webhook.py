"""The guarded deploy webhook: authentication per provider, branch filter, locks, secrets."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
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


# -- one key per service ---------------------------------------------------------------------------

SECOND = """\
  - slug: other
    name: other
    parent: portainer-vm
    controller: {type: portainer, endpoint: 1, stack: media}
    deploy:
      trigger: {webhook: {provider: generic, branch: ""}}
      guard: {pre: {probes: false}, post: {probes: false}}
"""


@pytest.fixture
def two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "services.yaml"
    path.write_text(
        REGISTRY.replace("PROVIDER", "gitlab").replace(
            "  - slug: media\n", SECOND + "  - slug: media\n"
        )
    )
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    monkeypatch.setenv("ORCHESTRATOR_PUBLIC_URL", "https://orch.example.lan")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_every_service_has_its_own_key(two: TestClient) -> None:
    a = new_secret(two)
    b = two.post("/api/services/other/deploy/webhook").json()["secret"]
    assert a != b
    # a key opens its own service only - for a deploy and for reading a run
    assert (
        two.post("/hooks/deploy/other", headers={"Authorization": f"Bearer {a}"}).status_code == 401
    )
    assert (
        two.post("/hooks/deploy/dev-xymbols", headers={"Authorization": f"Bearer {b}"}).status_code
        == 401
    )
    ok = two.post("/hooks/deploy/dev-xymbols", headers={"Authorization": f"Bearer {a}"}, json={})
    assert ok.status_code == 202
    run_id = ok.json()["run_id"]
    wait_run(two, run_id)
    assert (
        two.get(
            f"/hooks/deploy/dev-xymbols/runs/{run_id}", headers={"Authorization": f"Bearer {b}"}
        ).status_code
        == 401
    )
    # ...and the other service cannot read this service's run even with its own key
    assert (
        two.get(
            f"/hooks/deploy/other/runs/{run_id}", headers={"Authorization": f"Bearer {b}"}
        ).status_code
        == 404
    )
    assert two.get(f"/hooks/deploy/dev-xymbols/runs/{run_id}").status_code == 401
    assert (
        two.post("/api/services/other/deploy/webhook").json()["secret"] != b
    )  # replacing gives a new one


def _bearer_works(client: TestClient) -> None:
    secret = new_secret(client)
    r = client.post(
        "/hooks/deploy/dev-xymbols",
        headers={"Authorization": f"Bearer {secret}"},
        json={"ref": "refs/heads/dev"},
    )
    assert r.status_code == 202 and r.json()["started"] is True, r.text
    wait_run(client, r.json()["run_id"])
    assert (
        client.post(
            "/hooks/deploy/dev-xymbols", headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )


def test_the_bearer_key_works_for_a_gitlab_service(gitlab: TestClient) -> None:
    _bearer_works(gitlab)


def test_the_bearer_key_works_for_a_github_service(github: TestClient) -> None:
    _bearer_works(github)


def test_run_status_shows_steps_and_reasons_but_nothing_else(generic: TestClient) -> None:
    secret = new_secret(generic)
    HEALTH["dev-xymbols"] = HealthState.FAILED
    r = generic.post("/hooks/deploy/dev-xymbols", headers={"Authorization": f"Bearer {secret}"})
    wait_run(generic, r.json()["run_id"])
    body = generic.get(
        f"/hooks/deploy/dev-xymbols/runs/{r.json()['run_id']}",
        headers={"Authorization": f"Bearer {secret}"},
    ).json()
    assert body["status"] == "skipped" and "before the deploy" in body["error"]
    assert {"name", "status", "message"} == set(body["steps"][0])
    assert any(s["status"] == "failed" and s["message"] for s in body["steps"])
    assert (
        generic.get(
            "/hooks/deploy/dev-xymbols/runs/9999", headers={"Authorization": f"Bearer {secret}"}
        ).status_code
        == 404
    )
    assert (
        generic.get(
            "/hooks/deploy/media/runs/1", headers={"Authorization": f"Bearer {secret}"}
        ).status_code
        == 404
    )


# -- the GitHub pipeline block -----------------------------------------------------------------------


def test_the_github_block_uses_the_repository_secret_not_the_key(generic: TestClient) -> None:
    import yaml

    before = generic.get("/api/services/dev-xymbols/deploy").json()["webhook"]
    assert before["github"]["secret_name"] == "DEPLOY_KEY_DEV_XYMBOLS"
    created = generic.post("/api/services/dev-xymbols/deploy/webhook").json()
    secret = created["secret"]
    shown = generic.get("/api/services/dev-xymbols/deploy")
    assert secret not in shown.text  # never again after creation
    text = shown.json()["webhook"]["github"]["workflow"]
    doc = yaml.safe_load(text)
    assert doc["name"] == "Deploy dev-xymbols"
    assert doc[True]["push"][
        "branches"
    ]  # YAML 1.1 reads `on` as true == ["main"]  # no branch set: main, the user edits it
    env = doc["jobs"]["deploy"]["steps"][0]["env"]
    assert env["DEPLOY_URL"] == "https://orch.example.lan/hooks/deploy/dev-xymbols"
    assert env["DEPLOY_KEY"] == "${{ secrets.DEPLOY_KEY_DEV_XYMBOLS }}"
    # the version with the key in it exists only in the response that created the key
    assert secret in created["github_inline_key"] and secret not in created["github"]["workflow"]
    assert (
        yaml.safe_load(created["github_inline_key"])["jobs"]["deploy"]["steps"][0]["env"][
            "DEPLOY_KEY"
        ]
        == secret
    )


def test_the_block_names_the_configured_branch(gitlab: TestClient) -> None:
    import yaml

    text = gitlab.get("/api/services/dev-xymbols/deploy").json()["webhook"]["github"]["workflow"]
    assert yaml.safe_load(text)[True]["push"]["branches"] == ["dev"]


# -- the block, run for real ---------------------------------------------------------------------------


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The app on a real port, so the generated script can curl it."""
    import socket
    import threading

    import httpx
    import uvicorn

    from tests import test_proxy_hosts as th

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base = f"http://127.0.0.1:{port}"
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY.replace("PROVIDER", "github"))
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    monkeypatch.setenv("ORCHESTRATOR_PUBLIC_URL", base)
    monkeypatch.setenv("WEBAUTHN_ORIGIN", base)
    monkeypatch.setenv("WEBAUTHN_RP_ID", "127.0.0.1")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    th._fake_verify_registration(monkeypatch, b"cred-1")
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            httpx.get(base + "/healthz", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    admin = httpx.Client(base_url=base)
    assert (
        admin.post(
            "/api/auth/register/begin", json={"username": "storm", "invite_token": None}
        ).status_code
        == 200
    )
    reg = admin.post(
        "/api/auth/register/complete",
        json={
            "username": "storm",
            "device_label": "t",
            "invite_token": None,
            "credential": th._credential(b"cred-1"),
        },
    )
    assert reg.status_code == 200, reg.text
    yield admin, base
    server.should_exit = True
    thread.join(5)


def _run_block(
    admin: Any, base: str, key: str | None, *, url: str | None = None
) -> tuple[int, str]:
    """Run the generated workflow's script the way a runner would."""
    import subprocess

    import yaml

    text = admin.get("/api/services/dev-xymbols/deploy").json()["webhook"]["github"]["workflow"]
    step = yaml.safe_load(text)["jobs"]["deploy"]["steps"][0]
    env = {
        "PATH": os.environ["PATH"],
        "DEPLOY_URL": url or f"{base}/hooks/deploy/dev-xymbols",
        "DEPLOY_KEY": key or "",
        "REF": "refs/heads/dev",
        "SHA": "abcdef0123456789",
        "ACTOR": "storm",
        "DEPLOY_POLL_S": "1",
    }
    done = subprocess.run(
        ["bash", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=os.environ.get("TMPDIR", "/tmp"),
    )
    return done.returncode, done.stdout + done.stderr


@pytest.mark.skipif(not (shutil.which("jq") and shutil.which("curl")), reason="needs jq and curl")
def test_the_block_succeeds_when_the_deploy_is_healthy(live: Any) -> None:
    admin, base = live
    key = admin.post("/api/services/dev-xymbols/deploy/webhook").json()["secret"]
    code, out = _run_block(admin, base, key)
    assert code == 0, out
    assert "started" in out and "Deployed and healthy" in out


@pytest.mark.skipif(not (shutil.which("jq") and shutil.which("curl")), reason="needs jq and curl")
def test_the_block_reports_every_way_it_can_go_wrong(live: Any) -> None:
    admin, base = live
    key = admin.post("/api/services/dev-xymbols/deploy/webhook").json()["secret"]

    code, out = _run_block(admin, base, None)
    assert code == 1 and "key is empty" in out and "DEPLOY_KEY_DEV_XYMBOLS" in out

    code, out = _run_block(admin, base, "wrong")
    assert code == 1 and "rejected the key (401)" in out

    code, out = _run_block(admin, base, key, url="http://127.0.0.1:1/hooks/deploy/dev-xymbols")
    assert code == 1 and "Could not reach the orchestrator" in out

    code, out = _run_block(admin, base, key, url=f"{base}/hooks/deploy/ghost")
    assert code == 1 and "no deploy webhook" in out

    HEALTH["dev-xymbols"] = HealthState.FAILED  # a guard says no
    code, out = _run_block(admin, base, key)
    assert code == 1 and "Blocked before anything changed" in out and "before the deploy" in out
    HEALTH.clear()

    assert admin.post("/api/services/dev-xymbols/switch", json={"state": "off"}).status_code == 200
    code, out = _run_block(admin, base, key)
    assert code == 1 and "switched off in the orchestrator (423)" in out
    assert admin.post("/api/services/dev-xymbols/switch", json={"state": "on"}).status_code == 200


@pytest.mark.skipif(not (shutil.which("jq") and shutil.which("curl")), reason="needs jq and curl")
def test_the_block_fails_the_job_when_the_deploy_goes_wrong_after_the_redeploy(
    live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, base = live
    key = admin.post("/api/services/dev-xymbols/deploy/webhook").json()["secret"]
    calls: list[str] = []

    async def scan(self: Any, session: Any, service: Any, **kw: Any) -> ServiceVerdict:
        # healthy until the stack has been redeployed, then broken
        bad = bool(calls) and service.slug == "dev-xymbols"
        return ServiceVerdict(
            state=HealthState.FAILED if bad else HealthState.HEALTHY,
            reason="502 from the app",
            probe_results=[],
        )

    from orchestrator.deploy import pipeline as deploy_pipeline

    original = deploy_pipeline.DeployPipeline._redeploy

    async def redeploy(self: Any, *a: Any, **kw: Any) -> None:
        await original(self, *a, **kw)
        calls.append("redeployed")

    monkeypatch.setattr(HealthEngine, "scan", scan)
    monkeypatch.setattr(deploy_pipeline.DeployPipeline, "_redeploy", redeploy)
    code, out = _run_block(admin, base, key)
    assert code == 1, out
    assert "The deploy failed" in out and "502 from the app" in out


def test_cli_prints_the_github_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with make_client(tmp_path, monkeypatch, "github"):
        out = CliRunner().invoke(cli, ["deploy", "webhook", "dev-xymbols", "--github"])
        assert out.exit_code == 0, out.output
        assert ".github/workflows/deploy-dev-xymbols.yml" in out.output
        assert "secrets.DEPLOY_KEY_DEV_XYMBOLS" in out.output
        assert "https://orch.example.lan/hooks/deploy/dev-xymbols" in out.output
