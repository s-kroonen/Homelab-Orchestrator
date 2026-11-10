"""OR-77: the connector standard. Every kind is a connector, every connection a source, several
of a kind side by side, and nodes and proxy hosts know which source they belong to."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.factory import build_adapters
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest
from orchestrator.config import get_settings, reload_settings
from orchestrator.connections import store
from orchestrator.connections.model import Connection
from orchestrator.connectors import all_sources, connector_for, connectors, context, get_source
from orchestrator.connectors.kinds import raw_payload, redact
from orchestrator.connectors.ownership import claim_unowned, owned_by
from orchestrator.db import session as session_module
from orchestrator.db.models import Node, ProxyHost
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind


def _pve(ident: str, host: str, **kw: Any) -> Connection:
    return Connection(
        id=ident,
        kind="proxmox",
        settings={"host": host, "token_id": "orchestrator@pve!api"},
        secrets={"token_secret": "s3cr3t"},
        **kw,
    )


def _proxy(ident: str) -> Connection:
    return Connection(
        id=ident,
        kind="proxy",
        settings={"url": "https://traefik-manager.example.lan"},
        secrets={"api_key": "k"},
    )


def _use(conns: list[Connection]) -> Any:
    store.write_file(store.path_for(get_settings()), conns)
    return reload_settings()


def _cluster(adapter: Any, nodes: list[str], vmid0: int) -> None:
    adapter.simulated_cluster = ClusterStatus(nodes=[ClusterNode(n, True) for n in nodes])
    adapter.simulated_guests = [
        Guest(node=n, vmid=vmid0 + i, kind=GuestKind.VM, name=f"vm-{vmid0 + i}", status="running")
        for i, n in enumerate(nodes)
    ]


# -- the standard ----------------------------------------------------------------------------------


def test_every_connector_meets_the_standard() -> None:
    kinds = [c.kind for c in connectors()]
    assert len(kinds) == len(set(kinds))
    assert {"proxmox", "pbs", "portainer", "proxy", "power"} <= set(kinds)
    for c in connectors():
        assert c.title and c.group and c.family in {"machines", "apps", "network"}, c.kind
        views = [t.view for t in c.tabs()]
        assert views[0] == "overview" and views[-2:] == ["settings", "raw"], c.kind
        for r in c.resources:
            assert r.columns and callable(r.fetch), (c.kind, r.key)
            assert f"resource:{r.key}" in views
        for endpoint in c.raw:
            assert endpoint.describe and callable(endpoint.fetch)


def test_alert_channels_and_ssh_are_not_sources() -> None:
    for kind in ("ntfy", "email", "webhook", "ssh"):
        assert connector_for(kind) is None


# -- several of a kind -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_proxmox_clusters_each_list_their_own() -> None:
    settings = _use([_pve("cluster-a", "10.0.0.10", default=True), _pve("cluster-b", "10.0.1.10")])
    adapters = build_adapters(settings)
    assert [s.id for s in all_sources()] == ["cluster-a", "cluster-b"]

    a, b = adapters.for_source("cluster-a"), adapters.for_source("cluster-b")
    assert a is not None and b is not None and a is not b
    assert a is adapters.proxmox  # the default stands in for code that names none
    _cluster(a, ["node-a", "node-b"], 100)
    _cluster(b, ["node-c"], 200)

    pve = connector_for("proxmox")
    assert pve is not None
    nodes = pve.resource("nodes")
    assert nodes is not None
    for ident, want in (("cluster-a", ["node-a", "node-b"]), ("cluster-b", ["node-c"])):
        source = get_source(ident)
        assert source is not None
        ctx = context(source, settings, adapters)
        assert [n["name"] for n in await nodes.fetch(ctx)] == want
        summary = await pve.summary(ctx)
        assert summary.state == "unknown"  # dry run: nothing outside is contacted

    ctx_b = context(get_source("cluster-b"), settings, adapters)  # type: ignore[arg-type]
    guests = await pve.resource("guests").fetch(ctx_b)  # type: ignore[union-attr]
    assert [g["vmid"] for g in guests] == [200]


@pytest.mark.asyncio
async def test_raw_data_is_plain_json_without_credentials() -> None:
    settings = _use([_pve("cluster-a", "10.0.0.10")])
    adapters = build_adapters(settings)
    _cluster(adapters.proxmox, ["node-a"], 100)
    ctx = context(get_source("cluster-a"), settings, adapters)  # type: ignore[arg-type]
    for endpoint in connector_for("proxmox").raw:  # type: ignore[union-attr]
        json.dumps(await raw_payload(endpoint, ctx))
    assert redact({"user": "a", "password": "p", "nested": [{"api_key": "k", "n": 1}]}) == {
        "user": "a",
        "password": "•••",
        "nested": [{"api_key": "•••", "n": 1}],
    }


# -- ownership ------------------------------------------------------------------------------------------


def test_unowned_rows_go_to_the_default_source_and_owned_ones_stay() -> None:
    session_module.create_all_for_tests()
    _use(
        [
            _pve("cluster-a", "10.0.0.10", default=True),
            _pve("cluster-b", "10.0.1.10"),
            _proxy("gateway"),
        ]
    )
    with Session(get_engine()) as s:
        s.add(Node(name="node-a"))
        s.add(Node(name="node-c", source_id="cluster-b"))
        s.add(ProxyHost(hostname="app.example.lan"))
        s.commit()
        assert claim_unowned(s) == {"proxmox": 1, "proxy": 1}
        owners = {n.name: n.source_id for n in s.exec(select(Node)).all()}
        assert owners == {"node-a": "cluster-a", "node-c": "cluster-b"}
        assert s.exec(select(ProxyHost)).one().source_id == "gateway"
        assert [n.name for n in owned_by(s, Node, "cluster-b", default=False)] == ["node-c"]
        assert claim_unowned(s) == {}  # nothing left to claim
