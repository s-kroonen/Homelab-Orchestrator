"""Detection: the orchestrator's own network decides what "could not connect" means.

Regression: a node's network cable was pulled. The gateway, every proxy host and the
Proxmox API on that node stopped answering for hours, and nothing was announced: every
check that could not connect read UNKNOWN, UNKNOWN was never announced, and with the
Proxmox API gone no node was ever said to be down.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterUnreachable
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.config import get_settings
from orchestrator.db.models import (
    Node,
    NotificationRule,
    Probe,
    ProxyHost,
    Service,
    TargetState,
)
from orchestrator.domain.enums import GuestKind, HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult, ServiceVerdict
from orchestrator.health import network as net
from orchestrator.health.engine import HealthEngine, judge_unreachable
from orchestrator.health.guest_gate import GuestCheck, GuestStateCache, check_guest
from orchestrator.notify.monitor import collect
from orchestrator.registry import traefik_probes
from orchestrator.registry.tree import TreeIndex
from tests.test_notifications import Recorder, engine_with, feed, obs

# A TEST-NET-3 address (RFC 5737): never routed, so a connect never gets an answer.
NOWHERE = "203.0.113.7"


def _status(lan: bool | None = True, internet: bool | None = True, proxy: bool | None = None):
    checks = []
    for name, ok in (("lan", lan), ("internet", internet), ("proxy", proxy)):
        if ok is not None:
            checks.append(net.NetCheck(name, f"{name}.example.lan", ok, None, "no answer"))
    return net.NetworkStatus(checks=checks)


def _result(name: str, state: HealthState, **details: Any) -> ProbeResult:
    return ProbeResult(
        probe_name=name, kind=ProbeKind.HTTP, state=state, message=name, details=details
    )


def _row(name: str, required: bool = True, **kw: Any) -> Probe:
    return Probe(service_id=1, name=name, kind=ProbeKind.HTTP, required=required, config={}, **kw)


# -- the network checks ----------------------------------------------------------------------------


def _silent(monkeypatch: pytest.MonkeyPatch, *hosts: str) -> None:
    """Connections to ``hosts`` never get an answer (this sandbox's own network answers for
    every address, so a real unroutable one cannot be used)."""
    real = asyncio.open_connection

    async def connect(host: str, port: int, *a: Any, **kw: Any) -> Any:
        if host in hosts:
            raise TimeoutError
        return await real(host, port, *a, **kw)

    monkeypatch.setattr(net.asyncio, "open_connection", connect)


async def test_a_host_that_refuses_is_there_and_one_that_never_answers_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _silent(monkeypatch, NOWHERE)
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert (await net.reach(f"127.0.0.1:{port}"))[0] is True
    finally:
        server.close()
        await server.wait_closed()
    assert (await net.reach(f"127.0.0.1:{port}"))[0] is True  # refused: it answered
    ok, _ms, msg = await net.reach(f"{NOWHERE}:443", timeout=0.3)
    assert ok is False and msg


def test_the_lan_router_decides_whether_our_own_network_is_up() -> None:
    assert _status(lan=True, internet=False).own_network_ok is True
    assert _status(lan=False, internet=True).own_network_ok is False
    assert _status(lan=None, internet=True).own_network_ok is True  # anything that answered
    assert _status(lan=None, internet=None).own_network_ok is None  # nothing to go by


# -- what an unanswered check means ----------------------------------------------------------------


def _unknown(*results: ProbeResult) -> ServiceVerdict:
    return ServiceVerdict(
        state=HealthState.UNKNOWN, reason="indeterminate", probe_results=list(results)
    )


def test_unreachable_with_our_network_up_is_down_and_an_outage() -> None:
    gone = _result("web", HealthState.UNKNOWN, unreachable=True)
    v = judge_unreachable(_unknown(gone), [_row("web")], [gone], None, _status())
    assert v.state is HealthState.DOWN and v.unreachable and "not reachable" in v.reason


def test_unreachable_while_its_guest_runs_or_another_check_answers_is_failed() -> None:
    gone = _result("web", HealthState.UNKNOWN, unreachable=True)
    running = GuestCheck("up", "running")
    v = judge_unreachable(_unknown(gone), [_row("web")], [gone], running, _status())
    assert v.state is HealthState.FAILED and "guest is running" in v.reason
    ok = _result("db", HealthState.HEALTHY)
    rows = [_row("web"), _row("db")]
    v = judge_unreachable(_unknown(gone, ok), rows, [gone, ok], None, _status())
    assert v.state is HealthState.FAILED and "it answers" in v.reason


def test_with_our_own_network_down_nothing_can_be_concluded() -> None:
    gone = _result("web", HealthState.UNKNOWN, unreachable=True)
    v = judge_unreachable(_unknown(gone), [_row("web")], [gone], None, _status(lan=False))
    assert v.state is HealthState.UNKNOWN and v.cause == "network:lan"
    # and without any network check at all, the old rule stands
    assert judge_unreachable(_unknown(gone), [_row("web")], [gone], None, None).state is (
        HealthState.UNKNOWN
    )


def test_checks_through_a_dead_proxy_are_down_under_the_proxy() -> None:
    gone = _result("web", HealthState.UNKNOWN, unreachable=True, host_header="web.example.lan")
    v = judge_unreachable(_unknown(gone), [_row("web")], [gone], None, _status(proxy=False))
    assert v.state is HealthState.DOWN and v.cause == "network:proxy"


def test_a_check_that_could_not_run_stays_unknown() -> None:
    broken = _result("web", HealthState.UNKNOWN, config_error=True)
    v = judge_unreachable(_unknown(broken), [_row("web")], [broken], None, _status())
    assert v.state is HealthState.UNKNOWN


class _Scripted(HealthEngine):
    def __init__(self) -> None:
        super().__init__()
        self.ran: list[str] = []

    async def run_probe(self, row: Probe) -> ProbeResult:
        self.ran.append(row.name)
        return _result(row.name, HealthState.HEALTHY)


async def test_checks_that_need_the_internet_are_skipped_without_it(
    session: Session, service: Service
) -> None:
    session.add(
        Probe(service_id=service.id, name="local", kind=ProbeKind.TCP, required=True, config={})
    )
    session.add(Probe(service_id=service.id, name="public", kind=ProbeKind.HTTP, required=True,
                      config={}, needs_internet=True))  # fmt: skip
    session.commit()
    engine = _Scripted()
    v = await engine.scan(session, service, _network=_status(internet=False))
    assert engine.ran == ["local"] and v.state is HealthState.HEALTHY
    skipped = next(r for r in v.probe_results if r.probe_name == "public")
    assert skipped.details["skipped_because"] == "no_internet"


# -- nodes when no Proxmox API answers -------------------------------------------------------------


class _NoApi(DryRunProxmoxAdapter):
    async def cluster_status(self) -> ClusterStatus:
        raise AdapterUnreachable("pve unreachable")


async def test_with_no_proxmox_api_a_node_that_does_not_answer_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _silent(monkeypatch, NOWHERE)
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        cache = GuestStateCache(
            _NoApi(),
            nodes=[("node-a", f"127.0.0.1:{port}"), ("node-b", f"{NOWHERE}:8006")],
            own_network_ok=True,
        )
        cluster, err = await cache.cluster()
    finally:
        server.close()
        await server.wait_closed()
    assert err and cluster is not None and cluster.synthetic
    assert {n.name: n.online for n in cluster.nodes} == {"node-a": True, "node-b": False}
    gate = await check_guest(cache, node_name="node-b", guest_kind=GuestKind.VM, guest_id=101)
    assert gate.down and "does not answer" in gate.message


async def test_without_our_own_network_nodes_are_not_judged() -> None:
    cache = GuestStateCache(_NoApi(), nodes=[("node-b", f"{NOWHERE}:8006")], own_network_ok=False)
    cluster, _err = await cache.cluster()
    assert cluster is None


async def test_proxmox_falls_over_to_another_node(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.adapters import proxmox_family
    from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter

    asked: list[str] = []

    async def request(self: Any, method: str, path: str, **_kw: Any) -> Any:
        asked.append(self._base_url)
        if "10.0.0.1" in self._base_url:
            raise AdapterUnreachable("no route")
        return [
            {"type": "node", "name": "node-a", "online": 0},
            {"type": "node", "name": "node-b", "online": 1},
        ]

    monkeypatch.setenv("PROXMOX_HOST", "10.0.0.1")
    from orchestrator import config as config_module

    config_module.reset_settings_cache()
    monkeypatch.setattr(proxmox_family.ProxmoxFamilyClient, "request", request)
    adapter = LiveProxmoxAdapter(get_settings())
    adapter.set_fallback_hosts(["10.0.0.1", "10.0.0.2:8006"])

    cluster = await adapter.cluster_status()

    assert {n.name: n.online for n in cluster.nodes} == {"node-a": False, "node-b": True}
    assert asked == ["https://10.0.0.1:8006/api2/json", "https://10.0.0.2:8006/api2/json"]
    await adapter.cluster_status()  # the fallback is kept while the configured host is away
    assert asked[-1] == "https://10.0.0.2:8006/api2/json"


# -- what gets announced ---------------------------------------------------------------------------


def _tree(session: Session) -> tuple[TreeIndex, Service]:
    node = Node(name="node-a", address="10.0.0.11")
    session.add(node)
    session.commit()
    svc = Service(slug="web", name="Web", node_id=node.id, guest_kind=GuestKind.VM, guest_id=101)
    session.add(svc)
    session.commit()
    return TreeIndex.build(session), svc


def test_a_node_that_does_not_answer_is_down_and_the_network_is_observed(session: Session) -> None:
    tree, _svc = _tree(session)
    cluster = ClusterStatus(nodes=[ClusterNode("node-a", False, ip="10.0.0.11")], synthetic=True)
    out = collect(session, {}, cluster, tree, _status(lan=True, internet=False, proxy=True))
    by = {(o.level, o.key): o for o in out}
    assert (
        by[("node", "node-a")].state == "down"
        and "does not answer" in by[("node", "node-a")].reason
    )
    assert by[("network", "internet")].state == "down"
    assert by[("network", "lan")].state == "healthy"


def test_with_no_cluster_view_and_no_address_a_node_is_unknown(session: Session) -> None:
    session.add(Node(name="node-x"))
    session.commit()
    out = collect(session, {}, None, TreeIndex.build(session), _status())
    (node,) = [o for o in out if o.level == "node"]
    assert node.state == "unknown"


def test_an_unreachable_service_is_an_error_and_its_routes_follow_it(session: Session) -> None:
    tree, _svc = _tree(session)
    verdict = ServiceVerdict(
        state=HealthState.DOWN,
        reason="not reachable",
        unreachable=True,
        probe_results=[
            _result("traefik:web.example.lan", HealthState.UNKNOWN, unreachable=True),
            _result("public:web.example.lan", HealthState.FAILED),
        ],
    )
    out = collect(session, {"web": verdict}, None, tree, _status())
    svc_obs = next(o for o in out if o.level == "service")
    assert svc_obs.state == "down" and svc_obs.severity == "error"
    route = next(o for o in out if o.level == "route")
    assert route.key == "web.example.lan" and route.state == "down"
    assert route.cause_key == "service:web"  # the service behind it is the problem
    assert "inside, through the proxy" in route.reason and "from outside" in route.reason


def test_a_route_down_while_its_service_is_fine_is_its_own_alert(session: Session) -> None:
    tree, _svc = _tree(session)
    verdict = ServiceVerdict(
        state=HealthState.HEALTHY,
        reason="ok",
        probe_results=[
            _result("traefik:web.example.lan", HealthState.HEALTHY),
            _result("public:web.example.lan", HealthState.UNKNOWN, unreachable=True),
        ],
    )
    out = collect(session, {"web": verdict}, None, tree, _status())
    route = next(o for o in out if o.level == "route")
    assert route.state == "down" and route.cause_key is None


async def test_routes_and_the_network_are_errors_and_a_first_check_rule_sends_at_once(
    session: Session,
) -> None:
    rec = Recorder()
    engine = engine_with(rec)
    out = await feed(engine, session, [obs("web.example.lan", "healthy", level="route")],
                     *([[obs("web.example.lan", "down", level="route")]] * 2))  # fmt: skip
    (event,) = [e for batch in out for e in batch]
    assert event.severity == "error"

    session.add(NotificationRule(name="at once", first_check=True, levels=["network"]))
    session.commit()
    out = await feed(engine, session, [obs("lan", "healthy", level="network")],
                     [obs("lan", "down", level="network")])  # fmt: skip
    assert [len(x) for x in out] == [0, 1]  # on the first check that saw it
    assert session.get(TargetState, ("network", "lan")).state == "down"  # type: ignore[union-attr]


# -- checking a route from outside -----------------------------------------------------------------


def test_every_proxy_host_gets_an_outside_check_that_needs_the_internet(
    session: Session, service: Service
) -> None:
    traefik_probes.sync_traefik_probe(
        session, get_settings(), hostname="media.example.lan", service_id=service.id
    )
    (probe,) = session.exec(select(Probe).where(Probe.service_id == service.id)).all()
    assert probe.name == "public:media.example.lan" and probe.needs_internet is True
    assert probe.config["resolve_via"] == "1.1.1.1" and probe.required is False


def test_hosts_linked_before_it_existed_get_one_at_start(
    session: Session, service: Service
) -> None:
    session.add(ProxyHost(hostname="old.example.lan", service_id=service.id))
    session.commit()
    assert traefik_probes.backfill_public(session) == 1
    assert traefik_probes.backfill_public(session) == 0
    names = [p.name for p in session.exec(select(Probe)).all()]
    assert names == ["public:old.example.lan"]


async def test_the_outside_check_uses_public_dns_and_the_real_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.health.probes import network as probes

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/dns-query":
            body = {"Status": 0, "Answer": [{"type": 1, "data": "198.51.100.4"}]}
            if request.url.params["name"] == "gone.example.lan":
                body = {"Status": 3}
            return httpx.Response(200, content=json.dumps(body))
        return httpx.Response(302)

    real = httpx.AsyncClient

    def client(*a: Any, **kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(probes.httpx, "AsyncClient", client)
    probe = probes.HttpProbe()
    row = Probe(service_id=1, name="public:web.example.lan", kind=ProbeKind.HTTP, timeout_s=5,
                config=traefik_probes._public_config("web.example.lan"))  # fmt: skip

    result = await probe.run(row)

    assert result.state is HealthState.HEALTHY, result.message
    asked = seen[-1]
    assert asked.url.host == "198.51.100.4" and asked.headers["host"] == "web.example.lan"

    row.config = traefik_probes._public_config("gone.example.lan")
    skipped = await probe.run(row)
    assert skipped.details["skipped_because"] == "not_public"


# -- push ------------------------------------------------------------------------------------------


async def test_a_busy_database_never_turns_a_delivered_push_into_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.db import session as session_module
    from orchestrator.db.models import PushSubscription
    from orchestrator.db.session import get_engine, session_scope
    from orchestrator.notify import push

    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        s.add(
            PushSubscription(
                endpoint="https://push.example.test/a", p256dh="k", auth="a", label="Phone"
            )
        )
        s.commit()
    monkeypatch.setattr(push, "webpush", lambda **_kw: None)
    calls = {"n": 0}

    def factory() -> Any:
        calls["n"] += 1
        if calls["n"] > 1:  # the bookkeeping afterwards: the database is locked
            raise RuntimeError("database is locked")
        return session_scope()

    out = await push.deliver(factory, get_settings(), "{}")
    assert out == {"Phone": "ok"}
