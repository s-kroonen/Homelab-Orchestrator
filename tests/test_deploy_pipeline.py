"""The deploy pipeline: guards, redeploy, post checks, on_failure, locks, outside deploys."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.factory import AdapterBundle, build_adapters
from orchestrator.cli import cli
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import (
    AuditEntry,
    BackupRecord,
    PendingApproval,
    PipelineRun,
    Service,
)
from orchestrator.db.session import get_engine
from orchestrator.deploy import pipeline as deploy_pipeline
from orchestrator.deploy.pipeline import DeployPipeline, DeployRefused, Trigger
from orchestrator.deploy.watch import DeployWatcher, forget_all
from orchestrator.domain.enums import HealthState, PipelineKind, PipelineStatus
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
      guard:
        pre: {probes: true}
        post: {probes: true, timeout_s: 1, settle_s: 0}
  - slug: dev-xymbols-db
    name: database
    parent: dev-xymbols
    controller: {type: portainer, endpoint: 1, container: dev-xymbols-db-1}
  - slug: media
    name: media
    parent: portainer-vm
    controller: {type: portainer, endpoint: 1, stack: media}
"""

#: What the (patched) health engine answers, by slug. Anything missing is HEALTHY.
HEALTH: dict[str, HealthState] = {}
SCANS: list[str] = []


@pytest.fixture(autouse=True)
def fake_health(monkeypatch: pytest.MonkeyPatch) -> None:
    HEALTH.clear()
    SCANS.clear()
    forget_all()

    async def scan(self: Any, session: Any, service: Any, **kw: Any) -> ServiceVerdict:
        SCANS.append(service.slug)
        state = HEALTH.get(service.slug, HealthState.HEALTHY)
        return ServiceVerdict(
            state=state, reason=f"{service.slug} is {state.value}", probe_results=[]
        )

    monkeypatch.setattr(HealthEngine, "scan", scan)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AdapterBundle:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    return build_adapters(get_settings())


def _pipeline(adapters: AdapterBundle) -> DeployPipeline:
    return DeployPipeline(adapters, get_settings())


def _set_deploy(slug: str, **changes: Any) -> None:
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == slug)).one()
        cfg = dict(svc.deploy)
        for key, value in changes.items():
            cfg[key] = value
        svc.deploy = cfg
        s.add(svc)
        s.commit()


def _steps(run: PipelineRun) -> dict[str, str]:
    return {s["name"]: s["status"] for s in run.steps}


async def _deploy(adapters: AdapterBundle, **kw: Any) -> PipelineRun:
    with Session(get_engine()) as s:
        run = await _pipeline(adapters).run_for_service(
            s, kw.pop("slug", "dev-xymbols"), Trigger("cli", "tester"), **kw
        )
        s.refresh(run)
        return run


def _backup(age_h: float, *, slug: str = "portainer-vm", verified: bool = True) -> None:
    with Session(get_engine()) as s:
        s.add(
            BackupRecord(
                service_slug=slug,
                pbs_snapshot_id=f"vm/101/{age_h}",
                verified=verified,
                created_at=datetime.now(UTC) - timedelta(hours=age_h),
            )
        )
        s.commit()


# -- the happy path -------------------------------------------------------------------------


async def test_a_guarded_deploy_runs_every_step_in_order(env: AdapterBundle) -> None:
    portainer = env.portainer
    before = (await portainer.get_stack(1, "dev-xymbols")).git.hash  # type: ignore[union-attr]
    run = await _deploy(env)
    assert run.status is PipelineStatus.SUCCEEDED and run.error is None
    assert [s["name"] for s in run.steps] == [
        "trigger", "guard_probes", "guard_backup", "snapshot", "redeploy", "wait_up",
        "post_probes", "settle",
    ]  # fmt: skip
    assert _steps(run)["guard_backup"] == "skipped"  # not required here
    assert portainer.calls == ["redeploy dev-xymbols"]  # type: ignore[union-attr]
    after = (await portainer.get_stack(1, "dev-xymbols")).git.hash  # type: ignore[union-attr]
    assert after != before
    snap = next(s for s in run.steps if s["name"] == "snapshot")["detail"]
    assert snap["git_hash"] == before and {c["name"] for c in snap["containers"]} == {
        "dev-xymbols-web-1",
        "dev-xymbols-db-1",
    }
    # the service and its database container are both scanned before and after
    assert SCANS.count("dev-xymbols") >= 2 and "dev-xymbols-db" in SCANS
    with Session(get_engine()) as s:
        actions = [a.action for a in s.exec(select(AuditEntry)).all()]
    assert "deploy.start" in actions and "deploy.done" in actions


# -- pre-guards -----------------------------------------------------------------------------


async def test_unhealthy_before_the_deploy_blocks_it_and_changes_nothing(
    env: AdapterBundle,
) -> None:
    HEALTH["dev-xymbols-db"] = HealthState.FAILED
    run = await _deploy(env)
    assert run.status is PipelineStatus.SKIPPED
    assert "dev-xymbols-db is failed before the deploy" in (run.error or "")
    assert env.portainer.calls == []  # type: ignore[union-attr]
    assert "redeploy" not in _steps(run)


async def test_unknown_health_also_blocks_because_it_cannot_be_verified(env: AdapterBundle) -> None:
    HEALTH["dev-xymbols"] = HealthState.UNKNOWN
    assert (await _deploy(env)).status is PipelineStatus.SKIPPED


async def test_a_fresh_verified_backup_satisfies_the_backup_guard(env: AdapterBundle) -> None:
    _set_deploy(
        "dev-xymbols",
        guard={"pre": {"probes": False, "backup": {"max_age_h": 24, "take_if_older": False}}},
    )
    _backup(2)
    run = await _deploy(env)
    assert run.status is PipelineStatus.SUCCEEDED
    step = next(s for s in run.steps if s["name"] == "guard_backup")
    assert step["status"] == "ok" and step["detail"]["age_h"] == pytest.approx(2, abs=0.1)


async def test_an_old_backup_blocks_when_taking_one_is_not_allowed(env: AdapterBundle) -> None:
    _set_deploy(
        "dev-xymbols",
        guard={"pre": {"probes": False, "backup": {"max_age_h": 24, "take_if_older": False}}},
    )
    _backup(30)
    run = await _deploy(env)
    assert run.status is PipelineStatus.SKIPPED and "30h old" in (run.error or "")
    assert env.portainer.calls == []  # type: ignore[union-attr]


async def test_an_old_backup_is_replaced_by_a_new_one_when_allowed(
    env: AdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.pipelines.backup import BackupPipeline

    taken: list[str] = []

    async def fake_backup(self: Any, session: Session, slug: str, **kw: Any) -> PipelineRun:
        taken.append(slug)
        _backup(0.01)
        return PipelineRun(id=999, kind=PipelineKind.BACKUP, correlation_id="x")

    monkeypatch.setattr(BackupPipeline, "run_for_service", fake_backup)
    _set_deploy(
        "dev-xymbols",
        guard={"pre": {"probes": False, "backup": {"max_age_h": 24, "take_if_older": True}}},
    )
    run = await _deploy(env)
    assert run.status is PipelineStatus.SUCCEEDED
    assert taken == ["portainer-vm"]  # the guest the stack runs on
    assert next(s for s in run.steps if s["name"] == "guard_backup")["detail"]["took_backup"]


async def test_a_failed_backup_blocks_the_deploy(
    env: AdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.pipelines.backup import BackupPipeline
    from orchestrator.pipelines.steps import BackupError

    async def broken(self: Any, session: Session, slug: str, **kw: Any) -> PipelineRun:
        raise BackupError("PBS is full")

    monkeypatch.setattr(BackupPipeline, "run_for_service", broken)
    _set_deploy("dev-xymbols", guard={"pre": {"probes": False, "backup": {"max_age_h": 1}}})
    run = await _deploy(env)
    assert run.status is PipelineStatus.SKIPPED and "PBS is full" in (run.error or "")
    assert env.portainer.calls == []  # type: ignore[union-attr]


async def test_guards_only_reports_and_changes_nothing(env: AdapterBundle) -> None:
    _set_deploy(
        "dev-xymbols",
        guard={"pre": {"probes": True, "backup": {"max_age_h": 1, "take_if_older": True}}},
    )
    run = await _deploy(env, guards_only=True)
    assert run.status is PipelineStatus.SUCCEEDED
    assert (
        "would take one"
        in next(s for s in run.steps if s["name"] == "guard_backup")["detail"]["note"]
    )
    assert env.portainer.calls == [] and "redeploy" not in _steps(run)  # type: ignore[union-attr]


async def test_the_override_skips_the_pre_guards_and_is_audited(env: AdapterBundle) -> None:
    HEALTH["dev-xymbols"] = HealthState.FAILED  # would block a guarded deploy
    with Session(get_engine()) as s:
        run = await _pipeline(env).run_for_service(
            s, "dev-xymbols", Trigger("manual", "ops"), guards=False, reason="fixing a broken build"
        )
        s.refresh(run)
    # the post-guard still judges it - and the service really is failed
    assert _steps(run)["pre_guards"] == "skipped" and env.portainer.calls == ["redeploy dev-xymbols"]  # type: ignore[union-attr]
    with Session(get_engine()) as s:
        audit = [a for a in s.exec(select(AuditEntry)).all() if a.action == "deploy.unguarded"]
    assert len(audit) == 1 and audit[0].details["reason"] == "fixing a broken build"


# -- post-guards and on_failure ------------------------------------------------------------------


def _break_after_redeploy(env: AdapterBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scan(self: Any, session: Any, service: Any, **kw: Any) -> ServiceVerdict:
        redeployed = any(c.startswith("redeploy") for c in env.portainer.calls)  # type: ignore[union-attr]
        bad = redeployed and service.slug == "dev-xymbols"
        return ServiceVerdict(
            state=HealthState.FAILED if bad else HealthState.HEALTHY,
            reason="502 from the app" if bad else "ok",
            probe_results=[],
        )

    monkeypatch.setattr(HealthEngine, "scan", scan)


async def test_a_deploy_that_never_gets_healthy_fails_and_leaves_it_by_default(
    env: AdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_after_redeploy(env, monkeypatch)
    run = await _deploy(env)
    assert run.status is PipelineStatus.FAILED and "502 from the app" in (run.error or "")
    assert _steps(run)["on_failure"] == "ok"
    assert next(s for s in run.steps if s["name"] == "on_failure")["detail"]["action"] == "notify"
    assert (await env.portainer.get_stack(1, "dev-xymbols")).status == "active"  # type: ignore[union-attr]


async def test_on_failure_stop_switches_the_service_off(
    env: AdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_after_redeploy(env, monkeypatch)
    _set_deploy("dev-xymbols", on_failure="stop")
    run = await _deploy(env)
    assert run.status is PipelineStatus.FAILED
    step = next(s for s in run.steps if s["name"] == "on_failure")
    assert step["status"] == "ok" and step["detail"]["action"] == "stop"
    assert (await env.portainer.get_stack(1, "dev-xymbols")).status == "inactive"  # type: ignore[union-attr]
    with Session(get_engine()) as s:
        assert s.exec(select(Service).where(Service.slug == "dev-xymbols")).one().enabled is False
        kinds = [r.kind for r in s.exec(select(PipelineRun)).all()]
    assert kinds.count(PipelineKind.SWITCH) == 1  # the stop is its own, recorded switch


async def test_on_failure_restore_backup_only_proposes_it(
    env: AdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_after_redeploy(env, monkeypatch)
    _set_deploy("dev-xymbols", on_failure="restore_backup")
    _backup(3)
    run = await _deploy(env)
    assert run.status is PipelineStatus.FAILED
    step = next(s for s in run.steps if s["name"] == "on_failure")
    assert step["status"] == "ok" and step["detail"]["action"] == "restore_backup"
    with Session(get_engine()) as s:
        (approval,) = s.exec(select(PendingApproval)).all()
        assert approval.payload["service"] == "portainer-vm"  # a restore is the whole guest
        assert approval.status.value == "pending"
    # nothing was stopped or restored
    assert (await env.portainer.get_stack(1, "dev-xymbols")).status == "active"  # type: ignore[union-attr]


async def test_restore_backup_without_any_backup_says_so(
    env: AdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_after_redeploy(env, monkeypatch)
    _set_deploy("dev-xymbols", on_failure="restore_backup")
    run = await _deploy(env)
    step = next(s for s in run.steps if s["name"] == "on_failure")
    assert step["status"] == "failed" and "VERIFIED backup" in step["detail"]["error"]


async def test_portainer_refusing_the_redeploy_fails_the_run(env: AdapterBundle) -> None:
    env.portainer._read_only = True  # type: ignore[union-attr]
    run = await _deploy(env)
    assert run.status is PipelineStatus.FAILED and "read-only" in (run.error or "")


async def test_a_stack_that_is_not_from_git_cannot_be_redeployed(env: AdapterBundle) -> None:
    _set_deploy("dev-xymbols", guard={"pre": {"probes": False}, "post": {"probes": False}})
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == "dev-xymbols")).one()
        svc.controller = {"type": "portainer", "endpoint": 1, "stack": "media"}  # no Git repo
        s.add(svc)
        s.commit()
    run = await _deploy(env)
    assert run.status is PipelineStatus.FAILED and "Git repository" in (run.error or "")


# -- locks ------------------------------------------------------------------------------------


async def test_refusals_before_anything_is_recorded(env: AdapterBundle) -> None:
    pipeline = _pipeline(env)
    with Session(get_engine()) as s:
        for slug, code in (("ghost", "unknown"), ("media", "no_method")):
            with pytest.raises(DeployRefused) as caught:
                pipeline.start(s, slug, Trigger("manual", "t"))
            assert caught.value.code == code
        _set_deploy("dev-xymbols", enabled=False)
        with pytest.raises(DeployRefused) as caught:
            pipeline.start(s, "dev-xymbols", Trigger("manual", "t"))
        assert caught.value.code == "disabled"
        _set_deploy("dev-xymbols", enabled=True)

        svc = s.exec(select(Service).where(Service.slug == "dev-xymbols")).one()
        svc.enabled = False
        s.add(svc)
        s.commit()
        with pytest.raises(DeployRefused) as caught:
            pipeline.start(s, "dev-xymbols", Trigger("manual", "t"))
        assert caught.value.code == "off"
        assert s.exec(select(PipelineRun)).all() == []


async def test_only_one_deploy_at_a_time_and_not_during_a_backup(env: AdapterBundle) -> None:
    pipeline = _pipeline(env)
    with Session(get_engine()) as s:
        first = pipeline.start(s, "dev-xymbols", Trigger("manual", "t"))
        with pytest.raises(DeployRefused, match="already running"):
            pipeline.start(s, "dev-xymbols", Trigger("manual", "t"))
        first.status = PipelineStatus.SUCCEEDED
        s.add(first)
        root = s.exec(select(Service).where(Service.slug == "portainer-vm")).one()
        s.add(
            PipelineRun(
                kind=PipelineKind.BACKUP,
                correlation_id="b",
                service_id=root.id,
                status=PipelineStatus.RUNNING,
            )
        )
        s.commit()
        with pytest.raises(DeployRefused, match="backup of 'portainer-vm' is running"):
            pipeline.start(s, "dev-xymbols", Trigger("manual", "t"))


# -- deploys that happen outside ------------------------------------------------------------------


async def test_a_deploy_done_in_portainer_is_noticed_and_checked(env: AdapterBundle) -> None:
    watcher = DeployWatcher()
    portainer = env.portainer
    with Session(get_engine()) as s:
        assert await watcher.observe(s, env, get_settings()) == 0  # the baseline
        assert await watcher.observe(s, env, get_settings()) == 0  # nothing changed

    stack = await portainer.get_stack(1, "dev-xymbols")  # type: ignore[union-attr]
    await portainer.redeploy_stack(stack)  # type: ignore[union-attr]  # someone clicked it in Portainer
    portainer.calls.clear()  # type: ignore[union-attr]

    with Session(get_engine()) as s:
        assert await watcher.observe(s, env, get_settings()) == 1
    await asyncio.gather(*deploy_pipeline._background)
    with Session(get_engine()) as s:
        run = s.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.DEPLOY)).one()
    assert run.status is PipelineStatus.SUCCEEDED
    names = [x["name"] for x in run.steps]
    assert names[:2] == ["trigger", "outside_deploy"] and "post_probes" in names
    assert "redeploy" not in names  # it had already happened
    assert portainer.calls == []  # type: ignore[union-attr]  # and nothing was redeployed again
    assert next(x for x in run.steps if x["name"] == "trigger")["detail"]["source"] == "outside"
    # ...and it is only reported once
    with Session(get_engine()) as s:
        assert await watcher.observe(s, env, get_settings()) == 0


async def test_our_own_deploy_is_not_mistaken_for_an_outside_one(env: AdapterBundle) -> None:
    watcher = DeployWatcher()
    with Session(get_engine()) as s:
        await watcher.observe(s, env, get_settings())
    assert (await _deploy(env)).status is PipelineStatus.SUCCEEDED
    with Session(get_engine()) as s:
        assert await watcher.observe(s, env, get_settings()) == 0


async def test_stopping_and_starting_a_stack_is_not_a_deploy(env: AdapterBundle) -> None:
    watcher = DeployWatcher()
    portainer = env.portainer
    with Session(get_engine()) as s:
        await watcher.observe(s, env, get_settings())
    stack = await portainer.get_stack(1, "dev-xymbols")  # type: ignore[union-attr]
    await portainer.stop_stack(stack)  # type: ignore[union-attr]
    await portainer.start_stack(stack)  # type: ignore[union-attr]
    with Session(get_engine()) as s:
        assert await watcher.observe(s, env, get_settings()) == 0


# -- API and CLI ----------------------------------------------------------------------------------


@pytest.fixture
def client(env: AdapterBundle, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def _wait_for(client: TestClient, run_id: int) -> dict[str, Any]:
    import time

    for _ in range(200):
        body = client.get(f"/api/deploys/{run_id}").json()
        if body["status"] != "running":
            return body  # type: ignore[no-any-return]
        time.sleep(0.02)
    raise AssertionError("the deploy did not finish")


def test_api_starts_a_deploy_and_shows_its_history(client: TestClient) -> None:
    r = client.post("/api/services/dev-xymbols/deploy", json={})
    assert r.status_code == 202, r.text
    done = _wait_for(client, r.json()["run_id"])
    assert done["status"] == "succeeded" and done["service"] == "dev-xymbols"
    history = client.get("/api/services/dev-xymbols/deploys").json()
    assert [h["run_id"] for h in history] == [done["run_id"]]
    assert client.get("/api/deploys/9999").status_code == 404
    assert client.get("/api/services/ghost/deploys").status_code == 404


def test_api_refusals_use_the_right_codes(client: TestClient) -> None:
    assert client.post("/api/services/ghost/deploy", json={}).status_code == 404
    assert client.post("/api/services/media/deploy", json={}).status_code == 409
    unguarded = client.post("/api/services/dev-xymbols/deploy", json={"guards": False})
    assert unguarded.status_code == 400 and "reason" in unguarded.text
    assert client.post("/api/services/dev-xymbols/switch", json={"state": "off"}).status_code == 200
    locked = client.post("/api/services/dev-xymbols/deploy", json={})
    assert locked.status_code == 423 and "switched off" in locked.text


def test_cli_run_history_and_show(env: AdapterBundle) -> None:
    run = CliRunner()
    out = run.invoke(cli, ["deploy", "run", "dev-xymbols"])
    assert out.exit_code == 0, out.output
    assert "succeeded" in out.output and "redeploy" in out.output
    assert "#1" in run.invoke(cli, ["deploy", "history", "dev-xymbols"]).output
    shown = run.invoke(cli, ["deploy", "show", "1"])
    assert shown.exit_code == 0 and "post_probes" in shown.output

    HEALTH["dev-xymbols"] = HealthState.FAILED
    blocked = run.invoke(cli, ["deploy", "run", "dev-xymbols"])
    assert blocked.exit_code == 1 and "skipped" in blocked.output
    needs_reason = run.invoke(cli, ["deploy", "run", "dev-xymbols", "--unguarded"])
    assert needs_reason.exit_code != 0 and "--reason" in needs_reason.output
    refused = run.invoke(cli, ["deploy", "run", "media"])
    assert refused.exit_code != 0 and "no deploy method" in refused.output
