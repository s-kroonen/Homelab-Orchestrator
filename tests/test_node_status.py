"""GET /api/infra/nodes/status: the dashboard's live power-state column."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator.adapters.power.base import NodeReport
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus
from orchestrator.db import session as session_module
from orchestrator.db.models import Node
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import PowerState
from orchestrator.web.app import create_app
from orchestrator.web.routers.infra import combined_node_state
from tests.test_infra_pages import _login


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


@pytest.mark.parametrize(
    ("power", "online", "expected"),
    [
        (None, True, "on"),
        (PowerState.OFF, True, "on"),  # Proxmox serving it right now wins
        (PowerState.OFF, None, "off"),
        (PowerState.BOOTING, False, "booting"),
        (None, False, "off"),
        (PowerState.UNKNOWN, None, "unknown"),
        (None, None, "unknown"),
    ],
)
def test_combined_state(power: PowerState | None, online: bool | None, expected: str) -> None:
    assert combined_node_state(power, online) == expected


def _seed_nodes() -> None:
    with Session(get_engine()) as s:
        s.add(Node(name="gw", always_on=True, power_mgr_target=""))
        s.add(Node(name="compute", power_mgr_target="compute"))
        s.add(Node(name="storage", power_mgr_target="storage"))
        s.commit()


def test_endpoint_combines_power_manager_and_proxmox(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_nodes()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        adapters = app.state.adapters

        async def cluster() -> ClusterStatus:
            return ClusterStatus(
                nodes=[
                    ClusterNode("gw", online=True),
                    ClusterNode("compute", online=False),
                    ClusterNode("storage", online=False),
                ]
            )

        async def status(target: str) -> NodeReport:
            state = {"compute": PowerState.OFF, "storage": PowerState.UNKNOWN}[target]
            return NodeReport(node_name=target, state=state)

        monkeypatch.setattr(adapters.proxmox, "cluster_status", cluster)
        monkeypatch.setattr(adapters.power, "get_status", status)
        body = client.get("/api/infra/nodes/status").json()

    by = {n["name"]: n for n in body["nodes"]}
    assert by["gw"]["state"] == "on" and by["gw"]["power_detail"] == "no power target"
    assert by["compute"]["state"] == "off" and by["compute"]["power"] == "off"
    assert by["storage"]["state"] == "off"  # Proxmox says offline, power manager unsure
    assert body["proxmox_error"] is None


def test_unreachable_proxmox_and_power_errors_do_not_fail_the_call(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_nodes()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        adapters = app.state.adapters

        async def boom() -> ClusterStatus:
            raise RuntimeError("cluster down")

        async def status(target: str) -> NodeReport:
            raise RuntimeError("mqtt broker gone")

        monkeypatch.setattr(adapters.proxmox, "cluster_status", boom)
        monkeypatch.setattr(adapters.power, "get_status", status)
        r = client.get("/api/infra/nodes/status")

    assert r.status_code == 200
    body = r.json()
    assert body["proxmox_error"] == "cluster down"
    by = {n["name"]: n for n in body["nodes"]}
    assert by["compute"]["state"] == "unknown"
    assert by["compute"]["power_detail"] == "mqtt broker gone"


def test_dashboard_shows_each_nodes_state(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        assert client.get("/").status_code == 200
        js = client.get("/static/dashboard.js").text
    assert '"/api/infra/nodes/status"' in js
