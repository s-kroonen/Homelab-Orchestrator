"""``POST /api/services/{slug}/guest/power`` — raw Proxmox guest start/stop.

Deliberately NOT ``/wake/{slug}`` (the full orchestrated pipeline): this maps
straight through to ``adapters.proxmox.start_guest``/``stop_guest``, with a
guardrail refusing ``stop`` while a backup is currently RUNNING for the
service. Auth goes through the same mocked-WebAuthn helpers as
``test_auth.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.proxmox.base import TaskHandle
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, Node, PipelineRun, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, PipelineKind, PipelineStatus
from orchestrator.web.app import create_app
from tests.test_auth import _register


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        node = Node(name="eve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        s.add(
            Service(
                slug="thing", name="Thing", node_id=node.id, guest_kind=GuestKind.VM, guest_id=100
            )
        )
        s.add(Service(slug="no-guest", name="No Guest", node_id=node.id))
        s.commit()
    return create_app()


def _login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
    assert r.status_code == 200, r.text


def _audit_details() -> list[dict[str, Any]]:
    with Session(get_engine()) as s:
        rows = s.exec(
            select(AuditEntry).where(  # type: ignore[arg-type]
                AuditEntry.action == "service.guest_power_action"
            )
        ).all()
        return [r.details for r in rows]


def test_guest_power_requires_admin(app: Any) -> None:
    with TestClient(app) as client:
        r = client.post("/api/services/thing/guest/power", json={"action": "start"})
    assert r.status_code == 401


def test_start_maps_to_proxmox_start_guest(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        calls: list[tuple[str, int, GuestKind]] = []

        async def fake_start_guest(node: str, vmid: int, kind: GuestKind) -> TaskHandle:
            calls.append((node, vmid, kind))
            return TaskHandle(node=node, upid="UPID:eve1:started:")

        client.app.state.adapters.proxmox.start_guest = fake_start_guest  # type: ignore[attr-defined]
        r = client.post("/api/services/thing/guest/power", json={"action": "start"})

    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "ok",
        "service": "thing",
        "action": "start",
        "upid": "UPID:eve1:started:",
    }
    assert calls == [("eve1", 100, GuestKind.VM)]
    (details,) = _audit_details()
    assert details["action"] == "start"
    assert details["node"] == "eve1"
    assert details["vmid"] == 100


def test_stop_maps_to_proxmox_stop_guest(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        calls: list[tuple[str, int, GuestKind]] = []

        async def fake_stop_guest(node: str, vmid: int, kind: GuestKind) -> TaskHandle:
            calls.append((node, vmid, kind))
            return TaskHandle(node=node, upid="UPID:eve1:stopped:")

        client.app.state.adapters.proxmox.stop_guest = fake_stop_guest  # type: ignore[attr-defined]
        r = client.post("/api/services/thing/guest/power", json={"action": "stop"})

    assert r.status_code == 200, r.text
    assert calls == [("eve1", 100, GuestKind.VM)]
    (details,) = _audit_details()
    assert details["action"] == "stop"


def test_stop_refused_while_backup_running(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        with Session(get_engine()) as s:
            svc = s.exec(select(Service).where(Service.slug == "thing")).one()
            s.add(
                PipelineRun(
                    kind=PipelineKind.BACKUP,
                    correlation_id="corr-1",
                    service_id=svc.id,
                    status=PipelineStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
            )
            s.commit()

        r = client.post("/api/services/thing/guest/power", json={"action": "stop"})

    assert r.status_code == 409
    assert "backup is currently running" in r.text
    assert _audit_details() == []


def test_start_allowed_while_backup_running(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guardrail only blocks ``stop`` — a concurrent backup is no reason
    to refuse starting a guest back up."""
    with TestClient(app) as client:
        _login(client, monkeypatch)
        with Session(get_engine()) as s:
            svc = s.exec(select(Service).where(Service.slug == "thing")).one()
            s.add(
                PipelineRun(
                    kind=PipelineKind.BACKUP,
                    correlation_id="corr-1",
                    service_id=svc.id,
                    status=PipelineStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
            )
            s.commit()

        async def fake_start_guest(node: str, vmid: int, kind: GuestKind) -> TaskHandle:
            return TaskHandle(node=node, upid="UPID:eve1:started:")

        client.app.state.adapters.proxmox.start_guest = fake_start_guest  # type: ignore[attr-defined]
        r = client.post("/api/services/thing/guest/power", json={"action": "start"})

    assert r.status_code == 200, r.text


def test_stop_allowed_once_backup_no_longer_running(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        with Session(get_engine()) as s:
            svc = s.exec(select(Service).where(Service.slug == "thing")).one()
            s.add(
                PipelineRun(
                    kind=PipelineKind.BACKUP,
                    correlation_id="corr-1",
                    service_id=svc.id,
                    status=PipelineStatus.SUCCEEDED,  # finished, not RUNNING
                    started_at=datetime.now(UTC),
                )
            )
            s.commit()

        async def fake_stop_guest(node: str, vmid: int, kind: GuestKind) -> TaskHandle:
            return TaskHandle(node=node, upid="UPID:eve1:stopped:")

        client.app.state.adapters.proxmox.stop_guest = fake_stop_guest  # type: ignore[attr-defined]
        r = client.post("/api/services/thing/guest/power", json={"action": "stop"})

    assert r.status_code == 200, r.text


def test_unknown_service_404s(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.post("/api/services/does-not-exist/guest/power", json={"action": "start"})
    assert r.status_code == 404


def test_service_with_no_guest_400s(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.post("/api/services/no-guest/guest/power", json={"action": "start"})
    assert r.status_code == 400


def test_invalid_action_rejected(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.post("/api/services/thing/guest/power", json={"action": "pause"})
    assert r.status_code == 422


def test_adapter_error_maps_to_502(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)

        async def fake_start_guest(node: str, vmid: int, kind: GuestKind) -> TaskHandle:
            raise AdapterError("proxmox unreachable")

        client.app.state.adapters.proxmox.start_guest = fake_start_guest  # type: ignore[attr-defined]
        r = client.post("/api/services/thing/guest/power", json={"action": "start"})

    assert r.status_code == 502
    assert "proxmox unreachable" in r.text
    assert _audit_details() == []
