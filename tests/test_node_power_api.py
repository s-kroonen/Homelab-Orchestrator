"""``POST /api/infra/nodes/{name}/power`` — manual node power actions.

Deliberately NOT the wake pipeline: this maps straight through to
``adapters.power`` (wake/power_off/restart), with ``force`` passed through
exactly as given. Auth goes through the same mocked-WebAuthn helpers as
``test_auth.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, Node
from orchestrator.db.session import get_engine
from orchestrator.web.app import create_app
from tests.test_auth import _register


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        s.add(Node(name="eve1", always_on=True, power_mgr_target="eve1-topic"))
        s.commit()
    return create_app()


def _login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
    assert r.status_code == 200, r.text


def _audit_details() -> list[dict[str, Any]]:
    with Session(get_engine()) as s:
        rows = s.exec(
            select(AuditEntry).where(AuditEntry.action == "node.power_action")  # type: ignore[arg-type]
        ).all()
        return [r.details for r in rows]


def test_power_action_requires_admin(app: Any) -> None:
    with TestClient(app) as client:
        r = client.post("/api/infra/nodes/eve1/power", json={"action": "wake"})
    assert r.status_code == 401


def test_wake_maps_to_power_adapter_wake(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        calls: list[tuple[str, str]] = []

        async def fake_wake(node_name: str, *, reason: str) -> None:
            calls.append((node_name, reason))

        client.app.state.adapters.power.wake = fake_wake  # type: ignore[attr-defined]
        r = client.post("/api/infra/nodes/eve1/power", json={"action": "wake"})

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "node": "eve1", "action": "wake", "force": False}
    assert len(calls) == 1
    node_name, reason = calls[0]
    assert node_name == "eve1-topic"  # power_mgr_target, not the node's own name
    assert "storm" in reason
    assert _audit_details() == [{"action": "wake", "force": False}]


def test_power_off_passes_force_through_exactly(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        calls: list[tuple[str, str, bool]] = []

        async def fake_power_off(node_name: str, *, reason: str, force: bool = False) -> None:
            calls.append((node_name, reason, force))

        client.app.state.adapters.power.power_off = fake_power_off  # type: ignore[attr-defined]
        r = client.post("/api/infra/nodes/eve1/power", json={"action": "power_off", "force": True})

    assert r.status_code == 200, r.text
    assert calls == [("eve1-topic", calls[0][1], True)]
    assert _audit_details() == [{"action": "power_off", "force": True}]


def test_power_off_defaults_force_false(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        calls: list[tuple[str, str, bool]] = []

        async def fake_power_off(node_name: str, *, reason: str, force: bool = False) -> None:
            calls.append((node_name, reason, force))

        client.app.state.adapters.power.power_off = fake_power_off  # type: ignore[attr-defined]
        r = client.post("/api/infra/nodes/eve1/power", json={"action": "power_off"})

    assert r.status_code == 200, r.text
    assert calls[0][2] is False


def test_restart_maps_to_power_adapter_restart(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        calls: list[tuple[str, bool]] = []

        async def fake_restart(node_name: str, *, reason: str, force: bool = False) -> None:
            calls.append((node_name, force))

        client.app.state.adapters.power.restart = fake_restart  # type: ignore[attr-defined]
        r = client.post("/api/infra/nodes/eve1/power", json={"action": "restart", "force": True})

    assert r.status_code == 200, r.text
    assert calls == [("eve1-topic", True)]
    assert _audit_details() == [{"action": "restart", "force": True}]


def test_unknown_node_404s(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.post("/api/infra/nodes/does-not-exist/power", json={"action": "wake"})
    assert r.status_code == 404


def test_invalid_action_rejected(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.post("/api/infra/nodes/eve1/power", json={"action": "reboot"})
    assert r.status_code == 422


def test_adapter_error_maps_to_502(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)

        async def fake_wake(node_name: str, *, reason: str) -> None:
            raise AdapterError("power manager unreachable")

        client.app.state.adapters.power.wake = fake_wake  # type: ignore[attr-defined]
        r = client.post("/api/infra/nodes/eve1/power", json={"action": "wake"})

    assert r.status_code == 502
    assert "power manager unreachable" in r.text
    # A failed action is not recorded as a successful one.
    assert _audit_details() == []
