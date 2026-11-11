"""Editing a host with several backends through Traefik Manager's service form (OR-91)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orchestrator.adapters.proxy.base import RouteInfo
from orchestrator.registry import backend_edit as be
from tests.test_service_model import PRIMARY, RUNTIME, STANDBY


def _routes(service: str = "app-read") -> list[RouteInfo]:
    return [
        RouteInfo(
            name=service,
            hostnames={"app.example.lan"},
            upstream=None,
            service=service,
            rule="Host(`app.example.lan`)",
            priority=10,
        )
    ]


def _found(service: str = "app-read") -> be.Editable:
    return be.editable("app.example.lan", _routes(service), RUNTIME)


def _node(found: be.Editable, name: str) -> be.Node:
    return next(n for n in found.nodes if n.name == name)


def test_a_failover_host_lists_its_services_with_versions() -> None:
    found = _found()
    assert found.editable and found.strategy == "failover"
    assert [n.name for n in found.nodes] == ["app-read", "app-primary", "app-standby"]
    primary = _node(found, "app-primary")
    assert primary.kind == "loadBalancer" and primary.servers == [PRIMARY]
    # Traefik's defaults are not part of what would be written back; scheme is kept
    assert primary.health_check == {
        "path": "/api/health",
        "interval": "10s",
        "timeout": "3s",
        "scheme": "https",
        "hostname": "app.remote.example.lan",
    }
    assert _node(found, "app-read").primary == "app-primary"
    assert "last-resort" in found.choices and len(primary.version) == 16


def test_a_single_backend_host_is_not_edited_here() -> None:
    found = _found("app-standby")
    assert not found.editable and "one backend" in found.reason
    with pytest.raises(be.EditError, match="cannot be edited here"):
        be.plan(found, [{"service": "app-standby", "servers": ["http://10.0.0.5:80"]}], RUNTIME)


def test_swap_primary_and_fallback() -> None:
    found = _found()
    node = _node(found, "app-read")
    p = be.plan(
        found,
        [
            {
                "service": "app-read",
                "version": node.version,
                "primary": "app-standby",
                "fallback": "app-primary",
            }
        ],
        RUNTIME,
    )
    assert p.changes == [
        {
            "service": "app-read",
            "what": "failover",
            "before": {"primary": "app-primary", "fallback": "app-standby"},
            "after": {"primary": "app-standby", "fallback": "app-primary"},
        }
    ]
    assert p.calls == [
        {
            "service": "app-read",
            "adopt": True,
            "payload": {
                "name": "app-read",
                "type": "failover",
                "children": [
                    {"kind": "service", "name": "app-standby"},
                    {"kind": "service", "name": "app-primary"},
                ],
            },
        }
    ]


def test_change_a_backend_url_and_its_health_check() -> None:
    found = _found()
    p = be.plan(
        found,
        [
            {
                "service": "app-standby",
                "servers": ["http://10.0.0.13:2999"],
                "health_check": {"path": "/health", "interval": "15s"},
            }
        ],
        RUNTIME,
    )
    (call,) = p.calls
    assert call["adopt"] is False
    assert call["payload"] == {
        "name": "app-standby",
        "type": "loadBalancer",
        "children": [{"kind": "manual", "address": "http://10.0.0.13:2999"}],
        "healthCheck": {"enabled": True, "path": "/health", "interval": "15s"},
    }
    assert [c["what"] for c in p.changes] == ["servers", "health check"]


def test_a_health_check_edit_keeps_its_other_fields_and_can_remove_it() -> None:
    found = _found()
    p = be.plan(found, [{"service": "app-primary", "health_check": {"interval": "20s"}}], RUNTIME)
    sent = p.calls[0]["payload"]
    assert sent["healthCheck"] == {
        "enabled": True,
        "path": "/api/health",
        "interval": "20s",
        "timeout": "3s",
        "scheme": "https",
        "hostname": "app.remote.example.lan",
    }
    assert sent["children"] == [{"kind": "manual", "address": PRIMARY}]  # servers sent as they are
    off = be.plan(found, [{"service": "app-primary", "health_check": None}], RUNTIME)
    assert off.calls[0]["payload"]["healthCheck"] == {"enabled": False}


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"service": "app-standby", "servers": ["ftp://x"]}, "not an http"),
        ({"service": "app-standby", "servers": []}, "at least one server"),
        ({"service": "app-primary", "health_check": {"path": "health"}}, "must start with /"),
        ({"service": "app-primary", "health_check": {"interval": "often"}}, "duration"),
        ({"service": "app-read", "primary": "app-read"}, "its own primary"),
        ({"service": "app-read", "primary": "app-standby"}, "must be different"),
        ({"service": "app-read", "fallback": "nope"}, "not a service"),
        ({"service": "app-read", "fallback": "outer"}, "loops back"),
        ({"service": "elsewhere", "servers": ["http://10.0.0.1"]}, "not a service of"),
        ({"service": "app-read", "version": "0000000000000000", "primary": "x"}, "changed"),
        ({"service": "app-standby"}, "nothing to change"),
    ],
)
def test_bad_edits_are_refused_with_the_reason(edit: dict[str, Any], message: str) -> None:
    with pytest.raises(be.EditError, match=message):
        be.plan(_found(), [edit], RUNTIME)


def test_weights_and_mirrors() -> None:
    found = be.editable("app.example.lan", _routes("split"), RUNTIME)
    p = be.plan(found, [{"service": "split", "weights": {"app-standby": 1}}], RUNTIME)
    assert p.calls[0]["payload"]["children"] == [
        {"kind": "service", "name": "app-standby", "weight": 1},
        {"kind": "service", "name": "last-resort", "weight": 1},
    ]
    with pytest.raises(be.EditError, match="above 0"):
        be.plan(
            found, [{"service": "split", "weights": {"app-standby": 0, "last-resort": 0}}], RUNTIME
        )
    mirror = be.editable("app.example.lan", _routes("shadow"), RUNTIME)
    p = be.plan(mirror, [{"service": "shadow", "mirrors": {"last-resort": 25}}], RUNTIME)
    assert p.calls[0]["payload"]["children"] == [
        {"kind": "service", "name": "app-standby"},
        {"kind": "service", "name": "last-resort", "percent": 25},
    ]
    with pytest.raises(be.EditError, match="between 0 and 100"):
        be.plan(mirror, [{"service": "shadow", "mirrors": {"last-resort": 150}}], RUNTIME)


class FakeProxy:
    def __init__(self, refuse: str = "") -> None:
        self.calls: list[tuple[str, Any]] = []
        self.refuse = refuse

    async def adopt_service(self, name: str) -> None:
        self.calls.append(("adopt", name))

    async def save_service(self, payload: dict[str, Any]) -> None:
        if payload["name"] == self.refuse:
            raise RuntimeError("Traefik Manager returned 409: That service is not managed here")
        self.calls.append(("save", payload["name"]))


async def test_apply_adopts_composites_and_stops_at_a_refusal() -> None:
    found = _found()
    p = be.plan(
        found,
        [
            {"service": "app-read", "primary": "app-standby", "fallback": "app-primary"},
            {"service": "app-standby", "servers": ["http://10.0.0.13:2999"]},
        ],
        RUNTIME,
    )
    proxy = FakeProxy()
    results = await be.apply(proxy, p)
    assert proxy.calls == [("adopt", "app-read"), ("save", "app-read"), ("save", "app-standby")]
    assert all(r["ok"] for r in results)

    refused = FakeProxy(refuse="app-read")
    results = await be.apply(refused, p)
    assert results == [
        {
            "service": "app-read",
            "ok": False,
            "message": "Traefik Manager returned 409: That service is not managed here",
        }
    ]
    assert refused.calls == [("adopt", "app-read")]  # nothing after the refusal


async def test_verify_waits_for_traefik_to_show_the_change() -> None:
    found = _found()
    p = be.plan(found, [{"service": "app-standby", "servers": ["http://10.0.0.13:2999"]}], RUNTIME)
    after = copy.deepcopy(RUNTIME)
    for svc in after:
        if svc["name"] == "app-standby@file":
            svc["loadBalancer"]["servers"] = [{"url": "http://10.0.0.13:2999"}]
    reads = iter([RUNTIME, after])

    async def read() -> Any:
        return next(reads)

    assert await be.verify(read, "app.example.lan", _routes(), p, wait_s=0) is True

    async def never() -> Any:
        return RUNTIME

    assert await be.verify(never, "app.example.lan", _routes(), p, attempts=2, wait_s=0) is False
    assert STANDBY  # the fixture's standby is what was replaced


def test_the_api_previews_saves_and_reads_back(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /backends, a preview that sends nothing, a save through Traefik Manager's service
    form, Traefik read back, the edit audited; a stale version is refused."""
    import json

    import httpx

    from tests.test_proxy_hosts import _audit_actions_all, _patch_tm_transport, _tm_client

    client = _tm_client(
        monkeypatch, TRAEFIK_MANAGER_URL="http://tm.test:8080", TRAEFIK_MANAGER_API_KEY="key"
    )
    runtime = copy.deepcopy(RUNTIME)
    posted: list[tuple[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/routes":
            app = {
                "id": "app-read",
                "name": "app-read",
                "protocol": "http",
                "rule": "Host(`app.example.lan`)",
                "service_name": "app-read",
                "priority": 10,
            }
            return httpx.Response(200, json={"apps": [app], "middlewares": [], "configErrors": []})
        if path == "/api/traefik/services":
            return httpx.Response(200, json={"http": runtime, "reachable": True})
        if request.method == "POST":
            body = json.loads(request.read() or b"{}")
            posted.append((path, body))
            if path == "/api/services" and body.get("type") == "failover":
                for svc in runtime:  # Traefik reloads the file
                    if svc["name"] == "app-read@file":
                        kids = body["children"]
                        svc["failover"] = {"service": kids[0]["name"], "fallback": kids[1]["name"]}
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={})

    _patch_tm_transport(monkeypatch, handle)
    made = client.post(
        "/api/proxy-hosts", json={"hostname": "app.example.lan", "service_id": service.id}
    )
    assert made.status_code == 201, made.text
    host_id = made.json()["id"]

    got = client.get(f"/api/proxy-hosts/{host_id}/backends").json()
    assert got["editable"] and got["strategy"] == "failover"
    read = next(n for n in got["nodes"] if n["name"] == "app-read")
    swap = {
        "service": "app-read",
        "version": read["version"],
        "primary": "app-standby",
        "fallback": "app-primary",
    }

    preview = client.post(
        f"/api/proxy-hosts/{host_id}/backends", json={"edits": [swap], "preview": True}
    )
    assert preview.status_code == 200 and preview.json()["changes"][0]["what"] == "failover"
    assert posted == []  # a preview sends nothing

    done = client.post(f"/api/proxy-hosts/{host_id}/backends", json={"edits": [swap]})
    assert done.status_code == 200, done.text
    assert done.json()["verified"] is True
    assert [p for p, _ in posted] == ["/api/services/app-read/ownership", "/api/services"]
    assert "proxy_host.backends_edited" in _audit_actions_all()

    stale = client.post(f"/api/proxy-hosts/{host_id}/backends", json={"edits": [swap]})
    assert stale.status_code == 409 and "changed in Traefik" in stale.json()["detail"]
