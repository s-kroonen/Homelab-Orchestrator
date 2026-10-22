"""Automatic recovery (the ladder, its limits) and restore proposals."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.notifier.base import Notifier, NotifySeverity
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest
from orchestrator.config import get_settings
from orchestrator.controllers.base import ControllerError, ControllerStatus, ServiceController
from orchestrator.db import session as session_module
from orchestrator.db.models import (
    BackupRecord,
    PendingApproval,
    PipelineRun,
    RecoveryState,
    Service,
)
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import (
    ApprovalStatus,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.notify.engine import NotificationEngine
from orchestrator.recovery import engine as recovery_engine
from orchestrator.recovery.engine import RecoveryEngine
from orchestrator.recovery.policy import effective_max, is_enabled, normalize_policy
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.registry.tree import TreeIndex
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

# --- policy ---------------------------------------------------------------------


def test_policy_defaults_and_validation() -> None:
    assert normalize_policy({}) == {} and not is_enabled({})
    p = normalize_policy({"steps": ["restart", "restart_guest"]})
    assert (
        p["enabled"] and p["window_s"] == 3600 and p["settle_s"] == 120 and p["max_attempts"] == 0
    )
    assert effective_max(p, 2) == 2 and effective_max({**p, "max_attempts": 1}, 2) == 1
    assert effective_max({**p, "max_attempts": 9}, 2) == 2  # never more than the ladder
    assert not is_enabled({"enabled": False})
    for bad, message in (
        ({"colour": 1}, "unknown recovery key"),
        ({"steps": []}, "non-empty"),
        ({"steps": ["reboot_the_world"]}, "unknown recovery step"),
        ({"window_s": 5}, "at least 60"),
        ({"max_attempts": "lots"}, "whole number"),
        ({"enabled": "yes"}, "true or false"),
    ):
        with pytest.raises(ValueError, match=message):
            normalize_policy(bad)


# --- the engine --------------------------------------------------------------------

REGISTRY = """
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: docker-vm
    name: Docker VM
    node: pve1
    guest_kind: vm
    guest_id: 101
    controller: {type: docker, container: dockerd, transport: {type: local}}
  - slug: web
    name: web
    parent: docker-vm
    controller: {type: docker, container: web, transport: {type: local}}
    recovery: {steps: [restart, restart], max_attempts: 2, settle_s: 0, window_s: 3600}
  - slug: bare
    name: bare
    parent: docker-vm
    recovery: {steps: [restart]}
"""


class Rec(Notifier):
    def __init__(self) -> None:
        self.titles: list[str] = []
        self.urls: list[str | None] = []

    def channels(self) -> list[str]:
        return ["ntfy"]

    async def notify(self, **kw: Any) -> None:  # pragma: no cover
        raise AssertionError

    async def notify_via(
        self,
        channels: Any,
        *,
        severity: NotifySeverity,
        title: str,
        approve_url: str | None = None,
        **kw: Any,
    ) -> dict[str, str]:
        self.titles.append(title)
        self.urls.append(approve_url)
        return {"ntfy": "ok"}


class FakeController(ServiceController):
    type = "fake"
    calls: ClassVar[list[str]] = []

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name, self.fail = name, fail

    def describe(self) -> str:
        return self.name

    async def status(self) -> ControllerStatus:  # pragma: no cover
        return ControllerStatus(True, "running")

    async def start(self) -> str:  # pragma: no cover
        return "ok"

    async def stop(self) -> str:  # pragma: no cover
        return "ok"

    async def restart(self) -> str:
        FakeController.calls.append(self.name)
        if self.fail:
            raise ControllerError("boom")
        return f"restarted {self.name}"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    FakeController.calls = []

    def fake_build(
        settings: Any, proxmox: Any, session: Any, service: Service, *, target: str = "service"
    ) -> ServiceController:
        return FakeController(f"{service.slug}:{target}")

    monkeypatch.setattr(recovery_engine, "build_controller", fake_build)
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
        rec = Rec()
        settings = get_settings()
        engine = RecoveryEngine(
            settings=settings,
            proxmox=None,  # type: ignore[arg-type]
            notifications=NotificationEngine(rec, settings),
        )
        yield s, engine, rec


def v(state: HealthState, reason: str = "x") -> ServiceVerdict:
    return ServiceVerdict(state=state, reason=reason, probe_results=[])


async def cycle(s: Session, engine: RecoveryEngine, **states: HealthState) -> list[dict[str, Any]]:
    verdicts = {slug: v(state) for slug, state in states.items()}
    return await engine.run_cycle(s, verdicts, TreeIndex.build(s))


async def test_ladder_confirms_attempts_exhausts_and_resets(world: Any) -> None:
    s, engine, rec = world
    ok = HealthState.HEALTHY
    bad = HealthState.FAILED
    base = {"docker-vm": ok, "bare": ok}
    assert await cycle(s, engine, web=bad, **base) == []  # first FAILED scan: not yet
    first = await cycle(s, engine, web=bad, **base)
    assert [(r["event"], r["step"], r["ok"]) for r in first] == [("attempt", "restart", True)]
    assert FakeController.calls == ["web:service"]
    second = await cycle(s, engine, web=bad, **base)
    assert second[0]["step"] == "restart" and len(FakeController.calls) == 2
    done = await cycle(s, engine, web=bad, **base)
    assert [r["event"] for r in done] == ["exhausted"]
    assert await cycle(s, engine, web=bad, **base) == []  # silent after giving up
    assert len(FakeController.calls) == 2
    assert any("gave up" in t for t in rec.titles)

    back = await cycle(s, engine, web=ok, **base)
    assert back == [{"service": "web", "event": "recovered"}]
    assert any("is healthy again" in t for t in rec.titles)
    row = s.exec(select(RecoveryState)).first()
    assert row is not None and row.attempts == 0 and not row.exhausted


async def test_a_failed_action_still_counts_and_is_reported(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, engine, _rec = world
    monkeypatch.setattr(
        recovery_engine,
        "build_controller",
        lambda *a, **k: FakeController("web", fail=True),
    )
    base = {"docker-vm": HealthState.HEALTHY, "bare": HealthState.HEALTHY}
    await cycle(s, engine, web=HealthState.FAILED, **base)
    out = await cycle(s, engine, web=HealthState.FAILED, **base)
    assert out[0]["ok"] is False and out[0]["message"] == "boom"
    state = s.exec(select(RecoveryState)).first()
    assert state is not None and state.attempts == 1 and state.last_result.startswith("failed")


async def test_it_leaves_alone_what_is_not_its_to_fix(world: Any) -> None:
    s, engine, _ = world
    bad, ok = HealthState.FAILED, HealthState.HEALTHY
    # DOWN and UNKNOWN are not failures
    for state in (HealthState.DOWN, HealthState.UNKNOWN):
        for _ in range(3):
            assert await cycle(s, engine, web=state, **{"docker-vm": ok, "bare": ok}) == []
    # a failed parent: the child waits
    for _ in range(3):
        assert await cycle(s, engine, web=bad, **{"docker-vm": bad, "bare": ok}) == []
    # a service with no recovery block (docker-vm) is never touched, whatever it does
    for _ in range(3):
        assert await cycle(s, engine, web=ok, **{"docker-vm": bad, "bare": ok}) == []
    assert FakeController.calls == []


async def test_a_step_that_cannot_be_done_is_skipped_and_a_backup_blocks(world: Any) -> None:
    s, engine, _ = world
    bad, ok = HealthState.FAILED, HealthState.HEALTHY
    # "bare" has no controller: its only step ("restart") does not apply -> exhausted at once
    await cycle(s, engine, bare=bad, **{"docker-vm": ok, "web": ok})
    out = await cycle(s, engine, bare=bad, **{"docker-vm": ok, "web": ok})
    assert out[0]["event"] == "exhausted"

    root = s.exec(select(Service).where(Service.slug == "docker-vm")).one()
    s.add(
        PipelineRun(
            kind=PipelineKind.BACKUP,
            correlation_id="b",
            service_id=root.id,
            status=PipelineStatus.RUNNING,
        )
    )
    s.commit()
    for _ in range(3):
        assert await cycle(s, engine, web=bad, **{"docker-vm": ok, "bare": ok}) == []
    assert FakeController.calls == []


async def test_global_kill_switch(world: Any) -> None:
    s, engine, _ = world
    engine._settings = engine._settings.model_copy(update={"recovery_enabled": False})
    for _ in range(4):
        assert (
            await cycle(
                s,
                engine,
                web=HealthState.FAILED,
                **{"docker-vm": HealthState.HEALTHY, "bare": HealthState.HEALTHY},
            )
            == []
        )


async def test_propose_restore_creates_an_approval_and_a_linked_alert(world: Any) -> None:
    s, engine, rec = world
    root = s.exec(select(Service).where(Service.slug == "docker-vm")).one()
    web = s.exec(select(Service).where(Service.slug == "web")).one()
    web.recovery = normalize_policy({"steps": ["propose_restore"], "settle_s": 0})
    s.add(web)
    s.add(
        BackupRecord(
            service_id=root.id,
            service_slug="docker-vm",
            pbs_snapshot_id="vm/101/2026-10-01T02:00:00Z",
            verified=True,
        )
    )
    s.commit()
    engine._settings = engine._settings.model_copy(
        update={"external_url": "https://orch.example.com"}
    )
    base = {"docker-vm": HealthState.HEALTHY, "bare": HealthState.HEALTHY}
    await cycle(s, engine, web=HealthState.FAILED, **base)
    out = await cycle(s, engine, web=HealthState.FAILED, **base)
    assert out[0]["step"] == "propose_restore" and out[0]["ok"] is True
    approval = s.exec(select(PendingApproval)).one()
    assert approval.payload["service"] == "docker-vm" and approval.payload["snapshot"].startswith(
        "vm/101/"
    )
    assert approval.requested_by == "recovery" and approval.status is ApprovalStatus.PENDING
    assert f"https://orch.example.com/approvals/{approval.token}" in rec.urls
    assert any("Restore proposed" in t for t in rec.titles)


async def test_propose_restore_without_a_verified_backup_fails_clearly(world: Any) -> None:
    s, engine, _ = world
    web = s.exec(select(Service).where(Service.slug == "web")).one()
    web.recovery = normalize_policy({"steps": ["propose_restore"], "settle_s": 0})
    s.add(web)
    s.commit()
    base = {"docker-vm": HealthState.HEALTHY, "bare": HealthState.HEALTHY}
    await cycle(s, engine, web=HealthState.FAILED, **base)
    out = await cycle(s, engine, web=HealthState.FAILED, **base)
    assert out[0]["ok"] is False and "no VERIFIED backup" in out[0]["message"]


# --- restore proposals + approval page ---------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        root = s.exec(select(Service).where(Service.slug == "docker-vm")).one()
        s.add(
            BackupRecord(
                service_id=root.id,
                service_slug="docker-vm",
                pbs_snapshot_id="vm/101/2026-10-01T02:00:00Z",
                verified=True,
            )
        )
        s.commit()
    with TestClient(create_app()) as c:
        pve = c.app.state.adapters.proxmox  # type: ignore[attr-defined]
        pve.simulated_guests = [
            Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="d", status="running")
        ]
        pve.simulated_cluster = ClusterStatus(nodes=[ClusterNode("pve1", online=True)])
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_proposing_is_refused_where_it_makes_no_sense(client: TestClient) -> None:
    sub = client.post("/api/services/web/restore-proposal")
    assert sub.status_code == 400 and "sub-service" in sub.text
    assert client.post("/api/services/ghost/restore-proposal").status_code == 404
    with Session(get_engine()) as s:
        for r in s.exec(select(BackupRecord)).all():
            s.delete(r)
        s.commit()
    none = client.post("/api/services/docker-vm/restore-proposal")
    assert none.status_code == 400 and "VERIFIED backup" in none.text


def test_approving_restores_over_the_guest_and_reports_progress(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.health.engine import HealthEngine

    async def healthy(self: Any, session: Any, service: Any, **kw: Any) -> ServiceVerdict:
        return v(HealthState.HEALTHY, "fine")

    monkeypatch.setattr(HealthEngine, "scan", healthy)
    proposal = client.post("/api/services/docker-vm/restore-proposal")
    assert proposal.status_code == 201, proposal.text
    token = proposal.json()["token"]
    assert proposal.json()["open"] is True
    assert client.get(f"/approvals/{token}").status_code == 200
    assert client.get("/approvals/nope").status_code == 404

    run = client.post(f"/api/approvals/{token}/approve")
    assert run.status_code == 200, run.text
    deadline = time.time() + 10
    view = run.json()
    while time.time() < deadline and not (
        view["run"] and view["run"]["status"] in {"succeeded", "failed"}
    ):
        time.sleep(0.2)
        view = client.get(f"/api/approvals/{token}").json()
    assert view["run"]["status"] == "succeeded", view
    names = [step["name"] for step in view["run"]["steps"]]
    assert names[:4] == ["guest_stop", "restore", "guest_start", "health_poll"]
    assert view["status"] == "consumed"
    # a second approval of the same proposal is refused
    assert client.post(f"/api/approvals/{token}/approve").status_code == 409
    events = client.get("/api/notifications/events").json()
    assert {e["event"] for e in events} >= {"restore_proposed", "restore_succeeded"}


def test_deny_and_expiry(client: TestClient) -> None:
    token = client.post("/api/services/docker-vm/restore-proposal").json()["token"]
    denied = client.post(f"/api/approvals/{token}/deny").json()
    assert denied["status"] == "denied" and denied["open"] is False
    assert client.post(f"/api/approvals/{token}/approve").status_code == 409

    token2 = client.post("/api/services/docker-vm/restore-proposal").json()["token"]
    with Session(get_engine()) as s:
        row = s.exec(select(PendingApproval).where(PendingApproval.token == token2)).one()
        from datetime import UTC, datetime, timedelta

        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.add(row)
        s.commit()
    assert client.get(f"/api/approvals/{token2}").json()["status"] == "expired"
    assert client.post(f"/api/approvals/{token2}/approve").status_code == 409
    assert client.get("/api/approvals").status_code == 200


def test_a_new_proposal_supersedes_the_open_one(client: TestClient) -> None:
    a = client.post("/api/services/docker-vm/restore-proposal").json()["token"]
    b = client.post("/api/services/docker-vm/restore-proposal").json()["token"]
    assert client.get(f"/api/approvals/{a}").json()["status"] == "expired"
    assert client.get(f"/api/approvals/{b}").json()["open"] is True


def test_the_service_api_normalises_and_rejects_a_recovery_block(client: TestClient) -> None:
    bad = client.put("/api/registry/services/web", json={"recovery": {"steps": ["nuke"]}})
    assert bad.status_code == 400 and "unknown recovery step" in bad.text
    ok = client.put("/api/registry/services/web", json={"recovery": {"steps": ["restart"]}})
    assert ok.status_code == 200 and ok.json()["recovery"]["window_s"] == 3600
    off = client.put("/api/registry/services/web", json={"recovery": {}})
    assert off.json()["recovery"] == {}
