"""Per-node health through the node's own Proxmox API, address filling, and
the guest resync (whole, per node, one)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.adapters.proxmox.api import _split_address
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest, NodeHealth
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, Node, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.web.app import create_app
from orchestrator.web.routers.infra import combined_node_state
from tests.test_infra_pages import _login


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _seed() -> None:
    with Session(get_engine()) as s:
        a = Node(name="pve1", always_on=True, address="")
        b = Node(name="pve2", power_mgr_target="pve2", address="10.0.0.9:8006")
        c = Node(name="pve3", power_mgr_target="pve3")
        s.add_all([a, b, c])
        s.commit()
        for n in (a, b, c):
            s.refresh(n)
        s.add(
            Service(slug="media", name="Media", node_id=a.id, guest_kind=GuestKind.VM, guest_id=101)
        )
        s.add(
            Service(
                slug="photos", name="Photos", node_id=b.id, guest_kind=GuestKind.VM, guest_id=102
            )
        )
        s.commit()


def test_split_address() -> None:
    assert _split_address("10.0.0.5", 8006) == ("10.0.0.5", 8006)
    assert _split_address("pve1.lan:8007", 8006) == ("pve1.lan", 8007)
    assert _split_address("fe80::1", 8006) == ("fe80::1", 8006)


def test_reachable_api_counts_as_on() -> None:
    assert combined_node_state(None, None, True) == "on"
    assert combined_node_state(None, False, False) == "off"


def test_node_address_is_validated_and_stored(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        bad = client.put("/api/registry/nodes/pve3", json={"address": "https://pve3:8006/"})
        assert bad.status_code == 400
        bad = client.put("/api/registry/nodes/pve3", json={"address": "pve3:99999"})
        assert bad.status_code == 400
        ok = client.put("/api/registry/nodes/pve3", json={"address": " 10.0.0.7 "})
        assert ok.status_code == 200 and ok.json()["address"] == "10.0.0.7"


def test_check_node_and_status_include_health(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        calls: list[tuple[str, str]] = []

        async def health(node: str, address: str) -> NodeHealth:
            calls.append((node, address))
            return NodeHealth(reachable=False, error="connection refused")

        monkeypatch.setattr(app.state.adapters.proxmox, "node_health", health)
        one = client.post("/api/infra/nodes/pve2/check").json()
        assert one["reachable"] is False and one["error"] == "connection refused"
        assert calls == [("pve2", "10.0.0.9:8006")]

        # No address -> not asked, and said so.
        none = client.post("/api/infra/nodes/pve3/check").json()
        assert none["checked"] is False
        assert client.post("/api/infra/nodes/nope/check").status_code == 404

        status = client.get("/api/infra/nodes/status").json()["nodes"]
        by = {n["name"]: n for n in status}
        assert by["pve2"]["health"]["reachable"] is False
        assert by["pve1"]["health"]["checked"] is False

        checks = client.get("/api/infra/status").json()["checks"]
        row = next(c for c in checks if c["target"].startswith("node:pve2"))
        assert row["reachable"] is False and "connection refused" in row["error"]


def test_fill_addresses_uses_setup_host_for_the_local_node(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)

        async def cluster() -> ClusterStatus:
            return ClusterStatus(
                nodes=[
                    ClusterNode("pve1", online=True, local=True, ip="10.0.0.1"),
                    ClusterNode("pve2", online=True, ip="10.0.0.2"),  # already set by hand
                    ClusterNode("pve3", online=True, ip="10.0.0.3"),
                ]
            )

        monkeypatch.setattr(app.state.adapters.proxmox, "cluster_status", cluster)
        r = client.post("/api/infra/nodes/fill-addresses").json()
    from orchestrator.config import get_settings

    assert r["filled"] == {"pve1": get_settings().proxmox_host, "pve3": "10.0.0.3"}
    with Session(get_engine()) as s:
        assert s.exec(select(Node).where(Node.name == "pve2")).one().address == "10.0.0.9:8006"
        assert s.exec(select(AuditEntry).where(AuditEntry.action == "node.addresses_filled")).all()


def test_resync_whole_node_and_one(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        proxmox = app.state.adapters.proxmox
        proxmox.simulated_guests = [
            Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="media", status="running"),
            Guest(node="pve2", vmid=555, kind=GuestKind.VM, name="stray", status="stopped"),
        ]
        proxmox.simulated_cluster = ClusterStatus(
            nodes=[ClusterNode("pve1", online=True), ClusterNode("pve2", online=True)]
        )

        everything = client.post("/api/infra/resync", json={}).json()
        by = {r["slug"]: r for r in everything["results"]}
        assert by["media"]["guest"] == "running" and by["media"]["missing"] is False
        assert by["photos"]["missing"] is True  # vmid 102 is not in Proxmox
        assert everything["missing"] == ["photos"]
        assert [g["vmid"] for g in everything["unregistered_guests"]] == [555]

        node_only = client.post("/api/infra/resync", json={"node": "pve1"}).json()
        assert [r["slug"] for r in node_only["results"]] == ["media"]
        one = client.post("/api/infra/resync", json={"slug": "photos"}).json()
        assert [r["slug"] for r in one["results"]] == ["photos"]

        assert client.post("/api/infra/resync", json={"slug": "x", "node": "y"}).status_code == 400
        assert client.post("/api/infra/resync", json={"node": "nope"}).status_code == 404
        assert client.post("/api/infra/resync", json={"slug": "nope"}).status_code == 404

        # The dashboard column reads the stored result.
        page = client.get("/").text
        assert ">running</span>" in page

        bulk = client.post("/api/bulk/services", json={"action": "resync", "slugs": ["media"]})
        assert bulk.json()["ok_count"] == 1
        bulk_n = client.post("/api/bulk/nodes", json={"action": "resync", "names": ["pve2"]})
        assert bulk_n.json()["ok_count"] == 1
