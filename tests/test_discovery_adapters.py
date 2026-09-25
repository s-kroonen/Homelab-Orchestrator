"""The discovery calls the setup wizard relies on: nodes, storages, datastores."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from orchestrator.adapters.pbs.api import LivePbsAdapter
from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter
from orchestrator.config import Settings


def _settings() -> Settings:
    return Settings(
        proxmox_host="pve.test",
        proxmox_token_id="orch@pve!t",
        proxmox_token_secret=SecretStr("s"),
        pbs_host="pbs.test",
        pbs_token_id="orch@pbs!t",
        pbs_token_secret=SecretStr("s"),
    )


def _serve(adapter: Any, routes: dict[str, Any], seen: list[httpx.Request] | None = None) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path.split("/api2/json", 1)[1]
        if path not in routes:
            return httpx.Response(404)
        return httpx.Response(200, json={"data": routes[path]})

    adapter._client._client = httpx.AsyncClient(
        base_url=adapter._client._base_url, transport=httpx.MockTransport(handle)
    )


async def test_cluster_status_marks_the_local_node_and_quorum() -> None:
    adapter = LiveProxmoxAdapter(_settings())
    _serve(
        adapter,
        {
            "/cluster/status": [
                {"type": "cluster", "name": "lab", "quorate": 1, "nodes": 2},
                {"type": "node", "name": "pve2", "online": 0, "local": 0, "ip": "10.0.0.12"},
                {"type": "node", "name": "pve1", "online": 1, "local": 1, "ip": "10.0.0.11"},
            ]
        },
    )

    status = await adapter.cluster_status()

    assert [n.name for n in status.nodes] == ["pve1", "pve2"]
    assert status.local_node is not None and status.local_node.name == "pve1"
    assert status.nodes[1].online is False
    assert status.quorate is True
    assert status.cluster_name == "lab"


async def test_a_standalone_node_reports_no_quorum() -> None:
    adapter = LiveProxmoxAdapter(_settings())
    _serve(
        adapter,
        {"/cluster/status": [{"type": "node", "name": "pve", "online": 1, "local": 1}]},
    )

    status = await adapter.cluster_status()

    assert status.quorate is None
    assert status.local_node is not None and status.local_node.name == "pve"


async def test_backup_storages_are_pbs_entries_only() -> None:
    adapter = LiveProxmoxAdapter(_settings())
    seen: list[httpx.Request] = []
    _serve(
        adapter,
        {
            "/storage": [
                {
                    "storage": "pbs-main",
                    "type": "pbs",
                    "datastore": "store1",
                    "server": "10.0.0.20",
                },
                {"storage": "local", "type": "dir"},
            ]
        },
        seen,
    )

    storages = await adapter.list_backup_storages()

    assert [(s.storage, s.datastore, s.server) for s in storages] == [
        ("pbs-main", "store1", "10.0.0.20")
    ]
    assert seen[0].url.params["type"] == "pbs"


async def test_list_datastores_returns_sorted_names() -> None:
    adapter = LivePbsAdapter(_settings())
    _serve(adapter, {"/admin/datastore": [{"store": "b"}, {"store": "a", "comment": "x"}, {}]})

    assert await adapter.list_datastores() == ["a", "b"]
