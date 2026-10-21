"""Traefik Manager adapter: entry points / cert resolvers, runtime router
status, the explicit ``/save`` fields, and verification polling."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from orchestrator.adapters.proxy import verify
from orchestrator.adapters.proxy.base import EntryPointInfo, RouteInfo
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import (
    CERT_RESOLVER_DISABLED,
    CERT_RESOLVER_NONE,
    TraefikManagerProxyAdapter,
    cert_resolver_form_value,
)
from orchestrator.config import Settings

_REAL = httpx.AsyncClient

TRAEFIK_EPS = [
    {"address": ":80", "name": "web"},
    {"address": ":443", "name": "websecure"},
    {"address": ":8080", "name": "traefik"},
]


def _patch(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import orchestrator.adapters.proxy.traefik_manager as tm_mod

    class _Patched(_REAL):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tm_mod.httpx, "AsyncClient", _Patched)


def _settings(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {
        "traefik_manager_url": "http://tm.test:8080",
        "traefik_manager_api_key": "k",
        "orchestrator_public_url": "http://10.0.0.5:8000",
    }
    base.update(kwargs)
    return Settings(**base)


# -- /api/traefik/entrypoints ------------------------------------------------


async def test_list_entrypoints_parses_traefiks_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("x-api-key", "")
        return httpx.Response(200, json=TRAEFIK_EPS)

    _patch(monkeypatch, handle)
    eps = await TraefikManagerProxyAdapter(_settings()).list_entrypoints()
    assert seen == {"path": "/api/traefik/entrypoints", "key": "k"}
    assert eps == [
        EntryPointInfo("web", ":80"),
        EntryPointInfo("websecure", ":443"),
        EntryPointInfo("traefik", ":8080"),
    ]


async def test_list_entrypoints_502_api_unreachable_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(
        monkeypatch,
        lambda request: httpx.Response(502, json={"error": "Traefik API unreachable"}),
    )
    assert await TraefikManagerProxyAdapter(_settings()).list_entrypoints() is None


async def test_list_entrypoints_none_when_not_configured_or_unreachable_or_odd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await TraefikManagerProxyAdapter(_settings(traefik_manager_url="")).list_entrypoints()
        is None
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch(monkeypatch, refuse)
    assert await TraefikManagerProxyAdapter(_settings()).list_entrypoints() is None

    _patch(monkeypatch, lambda request: httpx.Response(200, json={"error": "x"}))
    assert await TraefikManagerProxyAdapter(_settings()).list_entrypoints() is None


async def test_list_entrypoints_skips_malformed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        lambda request: httpx.Response(200, json=[{"name": "web"}, "junk", {"address": ":1"}]),
    )
    assert await TraefikManagerProxyAdapter(_settings()).list_entrypoints() == [
        EntryPointInfo("web", "")
    ]


# -- /api/settings (cert resolvers) ------------------------------------------


async def test_list_cert_resolvers_reads_the_configured_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"cert_resolver": "cloudflare, letsencrypt,,none"}
        ),
    )
    assert await TraefikManagerProxyAdapter(_settings()).list_cert_resolvers() == [
        "cloudflare",
        "letsencrypt",
    ]


async def test_list_cert_resolvers_unknown_when_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, lambda request: httpx.Response(401, json={"error": "no"}))
    assert await TraefikManagerProxyAdapter(_settings()).list_cert_resolvers() is None
    assert (
        await TraefikManagerProxyAdapter(_settings(traefik_manager_url="")).list_cert_resolvers()
        is None
    )


# -- certResolver encoding ----------------------------------------------------


def test_cert_resolver_form_value_encoding() -> None:
    assert cert_resolver_form_value("letsencrypt", ["websecure"]) == "letsencrypt"
    # No resolver on a TLS entry point: keep the tls block, name no resolver.
    assert cert_resolver_form_value("", ["websecure"]) == CERT_RESOLVER_NONE
    # Plain HTTP: no tls block at all.
    assert cert_resolver_form_value("", ["web"]) == CERT_RESOLVER_DISABLED
    assert cert_resolver_form_value("", ["custom"], [EntryPointInfo("custom", ":80")]) == (
        CERT_RESOLVER_DISABLED
    )
    # Mixed: still TLS somewhere.
    assert cert_resolver_form_value("", ["web", "websecure"]) == CERT_RESOLVER_NONE


# -- explicit /save fields ----------------------------------------------------


def _save_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    saves: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/save":
            saves.append(dict(parse_qsl(request.read().decode())))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/traefik/entrypoints":
            return httpx.Response(200, json=TRAEFIK_EPS)
        if request.url.path == "/api/routes":
            # Whatever was saved is listed (an edit looks the route up first).
            apps = [
                {
                    "id": f["serviceName"],
                    "name": f["serviceName"],
                    "protocol": "http",
                    "rule": f["httpRule"],
                }
                for f in saves
            ]
            return httpx.Response(200, json={"apps": apps, "middlewares": [], "services": {}})
        return httpx.Response(404, json={"ok": False})

    _patch(monkeypatch, handle)
    return saves


async def test_create_and_update_always_send_entry_points_and_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saves = _save_recorder(monkeypatch)
    adapter = TraefikManagerProxyAdapter(_settings())
    ref = await adapter.create_route(
        hostname="a.example.com", upstream="http://10.0.0.9:80", entry_points=["web"]
    )
    await adapter.update_route(
        ref, hostname="a.example.com", upstream="http://10.0.0.9:80", entry_points=["websecure"]
    )
    create, update = saves
    assert create["entryPoints"] == "web" and create["certResolver"] == CERT_RESOLVER_DISABLED
    assert update["entryPoints"] == "websecure" and update["certResolver"] == CERT_RESOLVER_NONE
    for body in saves:
        assert body["entryPoints"].strip() and body["certResolver"].strip()


async def test_empty_entry_points_are_refused_and_nothing_is_posted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saves = _save_recorder(monkeypatch)
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(ValueError, match="entry point"):
        await adapter.create_route(hostname="a.example.com", upstream="http://x:1", entry_points=[])
    with pytest.raises(ValueError, match="entry point"):
        await adapter.update_route(
            "r", hostname="a.example.com", upstream="http://x:1", entry_points=["", "  "]
        )
    assert saves == []


# -- maintenance backend never posts a blank entry point ---------------------


async def test_maintenance_fallback_aborts_without_a_readable_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/routes":
            return httpx.Response(
                200, json={"apps": [], "middlewares": [], "services": {"http": []}}
            )
        if request.url.path == "/api/traefik/entrypoints":
            return httpx.Response(502, json={"error": "Traefik API unreachable"})
        return httpx.Response(200, json={"ok": True})

    _patch(monkeypatch, handle)
    events = await TraefikManagerProxyAdapter(_settings()).ensure_maintenance_fallback(
        hostname="a.example.com", service_slug="a"
    )
    assert events == []
    assert "/save" not in paths and "/save-middleware" not in paths


# -- runtime routers: status + error ------------------------------------------


async def test_runtime_routers_carry_status_error_and_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = {
        "name": "orchestrator-a-example-com@file",
        "rule": "Host(`a.example.com`)",
        "service": "orchestrator-a-example-com-service@file",
        "entryPoints": ["https"],
        "status": "disabled",
        "error": ['entryPoint "https" doesn\'t exist', "no valid entryPoint for this router"],
        "tls": {"certResolver": "letsencrypt"},
    }
    healthy = {
        "name": "b@file",
        "rule": "Host(`b.example.com`)",
        "entryPoints": ["websecure"],
        "status": "enabled",
    }
    _patch(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"http": [broken, healthy], "tcp": [], "udp": [], "reachable": True}
        ),
    )
    routers = await TraefikManagerProxyAdapter(_settings()).list_runtime_routers()
    assert routers is not None
    a, b = routers
    assert a.status == "disabled"
    assert a.error == 'entryPoint "https" doesn\'t exist · no valid entryPoint for this router'
    assert a.entry_points == ("https",) and a.cert_resolver == "letsencrypt"
    assert b.status == "enabled" and b.error is None


async def test_list_routes_carries_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "apps": [
                    {
                        "id": "r",
                        "name": "r",
                        "protocol": "http",
                        "rule": "Host(`a.example.com`)",
                        "entryPoints": ["web", "websecure"],
                    }
                ]
            },
        ),
    )
    routes = await TraefikManagerProxyAdapter(_settings()).list_routes()
    assert routes is not None and routes[0].entry_points == ("web", "websecure")


# -- verification polling -----------------------------------------------------


class _Scripted(DryRunProxyAdapter):
    """Returns a scripted sequence of runtime router lists."""

    def __init__(self, script: list[list[RouteInfo] | None]) -> None:
        super().__init__()
        self.script = script
        self.calls = 0

    async def list_runtime_routers(self) -> list[RouteInfo] | None:
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return item


def _router(
    name: str = "r@file",
    status: str | None = "enabled",
    error: str | None = None,
    eps=("websecure",),
) -> RouteInfo:
    return RouteInfo(
        name=name,
        hostnames={"a.example.com"},
        upstream=None,
        entry_points=eps,
        status=status,
        error=error,
    )


async def _noop_sleep(_s: float) -> None:
    return None


async def test_verify_ok_once_the_router_is_enabled() -> None:
    proxy = _Scripted([[], [_router()]])
    result = await verify.verify_route(
        proxy,
        route_ref="r",
        hostname="a.example.com",
        timeout_s=5,
        interval_s=0.5,
        sleep=_noop_sleep,
    )
    assert result.ok and proxy.calls == 2


async def test_verify_reports_traefiks_own_error_text() -> None:
    text = 'entryPoint "https" doesn\'t exist · no valid entryPoint for this router'
    proxy = _Scripted([[_router(status="disabled", error=text, eps=("https",))]])
    result = await verify.verify_route(
        proxy,
        route_ref="r",
        hostname="a.example.com",
        timeout_s=1,
        interval_s=0.5,
        sleep=_noop_sleep,
    )
    assert result.state == verify.ERROR and result.message == text
    assert proxy.calls == 3  # polled (0, 0.5, 1.0) before giving up


async def test_verify_disabled_without_error_text_still_fails() -> None:
    proxy = _Scripted([[_router(status="disabled")]])
    result = await verify.verify_route(
        proxy, route_ref="r", hostname="a.example.com", timeout_s=0, sleep=_noop_sleep
    )
    assert result.state == verify.ERROR and "disabled" in result.message


async def test_verify_unverified_when_runtime_list_unreadable_and_does_not_poll() -> None:
    proxy = _Scripted([None])
    result = await verify.verify_route(
        proxy, route_ref="r", hostname="a.example.com", timeout_s=5, sleep=_noop_sleep
    )
    assert result.state == verify.UNVERIFIED and proxy.calls == 1


async def test_verify_not_loaded_when_the_router_never_appears() -> None:
    proxy = _Scripted([[]])
    result = await verify.verify_route(
        proxy,
        route_ref="r",
        hostname="a.example.com",
        timeout_s=0.5,
        interval_s=0.5,
        sleep=_noop_sleep,
    )
    assert result.state == verify.NOT_LOADED


async def test_verify_waits_for_the_new_entry_points_to_load() -> None:
    old = _router(eps=("web",))
    new = _router(eps=("websecure",))
    proxy = _Scripted([[old], [new]])
    result = await verify.verify_route(
        proxy,
        route_ref="r",
        hostname="a.example.com",
        expect_entry_points=["websecure"],
        timeout_s=5,
        sleep=_noop_sleep,
    )
    assert result.ok and proxy.calls == 2


def test_match_runtime_router_prefers_the_stored_ref_then_the_hostname() -> None:
    other = RouteInfo(name="other@docker", hostnames={"a.example.com"}, upstream=None)
    ours = _router(name="r@file")
    assert (
        verify.match_runtime_router([other, ours], route_ref="r", hostname="a.example.com") is ours
    )
    assert verify.match_runtime_router([other], route_ref=None, hostname="a.example.com") is other
    assert (
        verify.match_runtime_router([other], route_ref="zzz", hostname="nope.example.com") is None
    )
