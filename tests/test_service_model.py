"""A route's service, resolved against Traefik's runtime services (OR-91).

Shapes taken from real hand-written setups, with example names: a read and a write router
by path, each a failover between a remote primary (own health check, Host header not
passed, a transport with its serverName) and a local standby without a check.
"""

from __future__ import annotations

from typing import Any

from orchestrator.adapters.proxy.base import RouteInfo
from orchestrator.registry.service_model import (
    FAILOVER,
    INTERNAL,
    MIRRORING,
    SINGLE,
    UNKNOWN,
    WEIGHTED,
    host_model,
    index_services,
    resolve,
)

PRIMARY = "https://10.0.1.20:443"
STANDBY = "http://10.0.0.12:2999"

RUNTIME: list[dict[str, Any]] = [
    {"name": "app-read@file", "failover": {"service": "app-primary", "fallback": "app-standby"}},
    {
        "name": "app-write@file",
        "failover": {"service": "app-primary-writable", "fallback": "app-standby"},
    },
    {
        "name": "app-primary@file",
        "loadBalancer": {
            "servers": [{"url": PRIMARY}],
            "passHostHeader": False,
            "serversTransport": "remote-tls@file",
            "healthCheck": {
                "path": "/api/health",
                "interval": "10s",
                "timeout": "3s",
                "hostname": "app.remote.example.lan",
                "scheme": "https",
            },
        },
        "serverStatus": {PRIMARY: "UP"},
    },
    {
        "name": "app-primary-writable@file",
        "loadBalancer": {
            "servers": [{"url": PRIMARY}],
            "passHostHeader": False,
            "healthCheck": {"path": "/api/health/writable", "interval": "10s"},
        },
        "serverStatus": {PRIMARY: "DOWN"},
    },
    {
        "name": "app-standby@file",
        "loadBalancer": {"servers": [{"url": STANDBY}], "passHostHeader": True},
    },
    # a failover whose primary is itself a failover
    {"name": "outer@file", "failover": {"service": "app-read", "fallback": "last-resort"}},
    {"name": "last-resort@file", "loadBalancer": {"servers": [{"url": "http://10.0.0.99:80"}]}},
    {"name": "loop-a@file", "failover": {"service": "loop-b", "fallback": "app-standby"}},
    {"name": "loop-b@file", "failover": {"service": "loop-a", "fallback": "app-standby"}},
    {
        "name": "split@file",
        "weighted": {
            "services": [{"name": "app-standby", "weight": 3}, {"name": "last-resort", "weight": 1}]
        },
    },
    {
        "name": "shadow@file",
        "mirroring": {
            "service": "app-standby",
            "mirrors": [{"name": "last-resort", "percent": 10}],
        },
    },
    {"name": "broken@file", "failover": {"service": "app-standby", "fallback": "gone"}},
]
TRANSPORTS = {
    "remote-tls@file": {"name": "remote-tls@file", "serverName": "app.remote.example.lan"}
}


def _services() -> dict[str, dict[str, Any]]:
    return index_services(RUNTIME)


def test_a_failover_shows_both_backends_with_their_roles_and_checks() -> None:
    m = resolve("app-read", _services(), TRANSPORTS)
    assert m.strategy == FAILOVER and m.service == "app-read@file"
    assert m.display == f"failover: {PRIMARY} -> {STANDBY}"
    primary, standby = m.upstreams
    assert (primary.role, primary.url, standby.role, standby.url) == (
        "primary",
        PRIMARY,
        "fallback",
        STANDBY,
    )
    assert primary.pass_host_header is False and standby.pass_host_header is True
    assert primary.health_check == {
        "path": "/api/health",
        "interval": "10s",
        "timeout": "3s",
        "hostname": "app.remote.example.lan",
    }
    assert standby.health_check is None  # deliberately unchecked
    assert primary.servers_transport == {
        "name": "remote-tls@file",
        "server_name": "app.remote.example.lan",
    }
    assert primary.status == "UP" and standby.status is None
    assert primary.via == ["app-read@file", "app-primary@file"]
    assert not m.problems


def test_without_traefiks_api_a_transport_is_known_by_name_only() -> None:
    (primary, _standby) = resolve("app-read", _services()).upstreams
    assert primary.servers_transport == {"name": "remote-tls@file"}


def test_nested_failovers_keep_every_role() -> None:
    m = resolve("outer", _services())
    assert [(u.role, u.url) for u in m.upstreams] == [
        ("primary > primary", PRIMARY),
        ("primary > fallback", STANDBY),
        ("fallback", "http://10.0.0.99:80"),
    ]
    assert m.display == f"failover: (failover: {PRIMARY} -> {STANDBY}) -> http://10.0.0.99:80"


def test_a_loop_is_reported_not_followed_forever() -> None:
    m = resolve("loop-a", _services())
    assert any("loops back" in p for p in m.problems)


def test_weighted_mirroring_internal_and_missing() -> None:
    w = resolve("split", _services())
    assert w.strategy == WEIGHTED
    assert [(u.role, u.weight) for u in w.upstreams] == [("member", 3), ("member", 1)]
    mi = resolve("shadow", _services())
    assert mi.strategy == MIRRORING
    assert [(u.role, u.percent) for u in mi.upstreams] == [("primary", None), ("mirror", 10)]
    assert resolve("prometheus@internal", _services()).strategy == INTERNAL
    assert resolve("nothing", _services()).strategy == UNKNOWN
    broken = resolve("broken", _services())
    assert broken.strategy == FAILOVER and any("gone" in p for p in broken.problems)
    assert resolve("app-standby", _services()).strategy == SINGLE


def _route(name: str, service: str, rule: str, priority: int | None) -> RouteInfo:
    return RouteInfo(
        name=name,
        hostnames={"app.example.lan"},
        upstream=None,
        service=service,
        rule=rule,
        priority=priority,
    )


def test_a_host_groups_its_routers_by_hostname_highest_priority_first() -> None:
    routes = [
        _route("app-read", "app-read", "Host(`app.example.lan`)", 10),
        _route(
            "app-write",
            "app-write",
            "Host(`app.example.lan`) && PathPrefix(`/api/write`)",
            100,
        ),
        _route("app-read-redirect", "noop@internal", "Host(`app.example.lan`)", None),
    ]
    host = host_model("app.example.lan", routes, _services(), TRANSPORTS)
    assert host is not None
    assert [r["router"] for r in host["routers"]] == ["app-write", "app-read", "app-read-redirect"]
    assert host["strategy"] == FAILOVER
    assert host["display"].startswith("app-write: failover: ")
    assert host["routers"][2]["redirect"] is True
    # unknown, not empty, when the proxy could not be read
    assert host_model("app.example.lan", None, _services()) is None
    assert host_model("app.example.lan", routes, None) is None


def test_a_list_gets_the_catch_all_router_and_a_count() -> None:
    routes = [
        _route("app-read", "app-read", "Host(`app.example.lan`)", 10),
        _route("app-write", "app-write", "Host(`app.example.lan`) && PathPrefix(`/w`)", 100),
    ]
    host = host_model("app.example.lan", routes, _services())
    assert host is not None
    assert host["short"] == f"failover: {PRIMARY} -> {STANDBY}" and host["other_routers"] == 1
