"""``TraefikManagerProxyAdapter``: the REST calls to Traefik Manager (auth
header, request shapes, failure degrade) and the maintenance-fallback
middleware's raw-YAML read-modify-write injection/removal — the one hard
requirement being that it survives every other field and middleware
untouched and is idempotent on re-run.

Ported from the old ``tests/test_traefik_manager_client.py`` +
``tests/test_traefik_manager_sync.py`` (now deleted — their logic moved
into ``orchestrator.adapters.proxy.traefik_manager``, see
``docs/roadmap.md``'s "generalize the adapter pattern" entry). Same
assertions, now made directly against the adapter's ``list_routes``/
``ensure_maintenance_fallback``/``remove_maintenance_fallback`` instead of
the old standalone client + sync function.

The create/rename/re-link/unlink ORCHESTRATION around these two calls (old
hostname/slug vs. new) now lives in ``web/routers/proxy_hosts.py``'s
``_sync_maintenance_fallback`` — exercised end-to-end in
``tests/test_proxy_hosts.py`` instead of here, since that's where the logic
now lives.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.adapters.proxy.base import ProxyRouteNotFound, RouteInfo
from orchestrator.adapters.proxy.traefik_manager import (
    SHARED_BACKEND_SERVICE_NAME,
    TraefikManagerProxyAdapter,
    maintenance_middleware_name,
    route_name_for_hostname,
)
from orchestrator.config import Settings

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import orchestrator.adapters.proxy.traefik_manager as tm_mod

    class _Patched(_REAL_ASYNC_CLIENT):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tm_mod.httpx, "AsyncClient", _Patched)


def _settings(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {
        "traefik_manager_url": "http://tm.test:8080",
        "traefik_manager_api_key": "secret-key",
        "orchestrator_public_url": "http://10.0.0.5:8000",
    }
    base.update(kwargs)
    return Settings(**base)


# ---------------------------------------------------------------------------
# list_routes — read path
# ---------------------------------------------------------------------------


async def test_list_routes_returns_none_when_not_configured() -> None:
    adapter = TraefikManagerProxyAdapter(_settings(traefik_manager_url=""))
    assert await adapter.list_routes() is None


async def test_list_routes_sends_api_key_header_and_parses_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["api_key"] = request.headers.get("X-Api-Key")
        return httpx.Response(
            200,
            json={
                "apps": [
                    {
                        "id": "r1",
                        "name": "r1",
                        "protocol": "http",
                        "rule": "Host(`a.example.com`)",
                        "servers": ["http://10.0.0.9:80"],
                    },
                    {
                        # Non-HTTP protocol — skipped.
                        "id": "tcp1",
                        "protocol": "tcp",
                        "rule": "HostSNI(`b.example.com`)",
                    },
                ],
                "middlewares": [],
                "services": {"http": [], "tcp": [], "udp": []},
            },
        )

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())
    routes = await adapter.list_routes()

    assert seen["path"] == "/api/routes"
    assert seen["api_key"] == "secret-key"
    assert routes == [
        RouteInfo(name="r1", hostnames={"a.example.com"}, upstream="http://10.0.0.9:80")
    ]


async def test_list_routes_returns_none_on_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())
    assert await adapter.list_routes() is None


async def test_list_routes_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings(traefik_manager_timeout_s=0.01))
    assert await adapter.list_routes() is None


async def test_list_routes_returns_none_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": "Not authenticated"})

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())
    assert await adapter.list_routes() is None


# ---------------------------------------------------------------------------
# Maintenance fallback — write path
# ---------------------------------------------------------------------------

RAW_ROUTE_WITH_EXTRAS = """\
http:
  routers:
    media-router:
      rule: Host(`media.example.com`)
      service: media-service
      entryPoints:
        - websecure
      middlewares:
        - chain-no-auth
      tls: {}
  services:
    media-service:
      loadBalancer:
        servers:
          - url: http://10.0.0.9:8096
"""


def _router_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stateful handler simulating just enough of Traefik Manager for the
    injection tests: GET /api/routes (one matching app), GET/POST the raw
    YAML for it, and the shared-backend-service existence check."""
    state: dict[str, Any] = {
        "raw": RAW_ROUTE_WITH_EXTRAS,
        "saved_raw_calls": [],
        "save_middleware_calls": [],
        "save_route_calls": [],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/routes":
            return httpx.Response(
                200,
                json={
                    "apps": [
                        {
                            "id": "media-router",
                            "name": "media-router",
                            "protocol": "http",
                            "rule": "Host(`media.example.com`)",
                        }
                    ],
                    "middlewares": [],
                    "services": {"http": [SHARED_BACKEND_SERVICE_NAME], "tcp": [], "udp": []},
                },
            )
        if path == "/api/routes/media-router/raw":
            if request.method == "GET":
                return httpx.Response(200, json={"raw": state["raw"], "configFile": "a.yml"})
            if request.method == "POST":
                state["content_type"] = request.headers.get("content-type", "")
                content = json.loads(request.read())["content"]
                state["raw"] = content
                state["saved_raw_calls"].append(content)
                return httpx.Response(200, json={"ok": True})
        if path == "/save-middleware":
            state["save_middleware_calls"].append(request.read().decode())
            state["save_middleware_headers"] = dict(request.headers)
            state["save_middleware_content_type"] = request.headers.get("content-type", "")
            return httpx.Response(200, json={"ok": True})
        if path == "/save":
            state["save_route_calls"].append(request.read().decode())
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)
    return state


async def test_ensure_maintenance_fallback_injects_preserving_every_other_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _router_calls(monkeypatch)
    adapter = TraefikManagerProxyAdapter(_settings())

    events = await adapter.ensure_maintenance_fallback(
        hostname="media.example.com", service_slug="media"
    )

    mw_name = maintenance_middleware_name("media")
    assert any(e.action == "middleware.injected" for e in events)
    assert len(state["saved_raw_calls"]) == 1
    assert "application/json" in state["content_type"]
    new_raw = state["saved_raw_calls"][0]

    # Every original field survives untouched...
    assert "rule: Host(`media.example.com`)" in new_raw
    assert "service: media-service" in new_raw
    assert "websecure" in new_raw
    assert "chain-no-auth" in new_raw  # the hand-authored middleware
    assert "tls: {}" in new_raw
    assert "media-service:" in new_raw
    assert "http://10.0.0.9:8096" in new_raw
    # ...and the new middleware name was appended, not used to replace
    # anything.
    assert mw_name in new_raw


async def test_ensure_maintenance_fallback_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _router_calls(monkeypatch)
    adapter = TraefikManagerProxyAdapter(_settings())

    await adapter.ensure_maintenance_fallback(hostname="media.example.com", service_slug="media")
    first_raw = state["raw"]

    events = await adapter.ensure_maintenance_fallback(
        hostname="media.example.com", service_slug="media"
    )

    # No second raw-YAML write — the middleware name was already present.
    assert len(state["saved_raw_calls"]) == 1
    assert state["raw"] == first_raw
    assert not any(e.action == "middleware.injected" for e in events)
    mw_name = maintenance_middleware_name("media")
    assert state["raw"].count(mw_name) == 1  # never duplicated


async def test_ensure_maintenance_fallback_no_op_when_traefik_manager_url_unset() -> None:
    adapter = TraefikManagerProxyAdapter(_settings(traefik_manager_url=""))
    events = await adapter.ensure_maintenance_fallback(
        hostname="media.example.com", service_slug="media"
    )
    assert events == []


async def test_ensure_maintenance_fallback_no_op_when_orchestrator_public_url_unset() -> None:
    adapter = TraefikManagerProxyAdapter(_settings(orchestrator_public_url=""))
    events = await adapter.ensure_maintenance_fallback(
        hostname="media.example.com", service_slug="media"
    )
    assert events == []


async def test_ensure_maintenance_fallback_unreachable_is_caught_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    # Must not raise — this is a side effect of an ordinary ProxyHost CRUD
    # request, not an explicit admin-triggered write like Resync.
    events = await adapter.ensure_maintenance_fallback(
        hostname="media.example.com", service_slug="media"
    )
    assert events == []


async def test_remove_maintenance_fallback_removes_injection_leaves_middleware_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mw_name = maintenance_middleware_name("media")
    raw = RAW_ROUTE_WITH_EXTRAS.replace(
        "      middlewares:\n        - chain-no-auth\n",
        f"      middlewares:\n        - chain-no-auth\n        - {mw_name}\n",
    )
    state = {"raw": raw}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/routes":
            return httpx.Response(
                200,
                json={
                    "apps": [
                        {
                            "id": "media-router",
                            "name": "media-router",
                            "protocol": "http",
                            "rule": "Host(`media.example.com`)",
                        }
                    ],
                    "middlewares": [{"name": mw_name}],
                    "services": {"http": [SHARED_BACKEND_SERVICE_NAME], "tcp": [], "udp": []},
                },
            )
        if path == "/api/routes/media-router/raw" and request.method == "GET":
            return httpx.Response(200, json={"raw": state["raw"]})
        if path == "/api/routes/media-router/raw" and request.method == "POST":
            state["raw"] = json.loads(request.read())["content"]
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    events = await adapter.remove_maintenance_fallback(
        hostname="media.example.com", service_slug="media"
    )

    assert mw_name not in state["raw"]
    assert "chain-no-auth" in state["raw"]  # untouched
    assert any(e.action == "middleware.removed" for e in events)
    # The middleware DEFINITION itself is never deleted by this adapter.
    assert not any("delete-middleware" in e.action for e in events)


async def test_remove_maintenance_fallback_no_op_when_traefik_manager_url_unset() -> None:
    adapter = TraefikManagerProxyAdapter(_settings(traefik_manager_url=""))
    events = await adapter.remove_maintenance_fallback(
        hostname="media.example.com", service_slug="media"
    )
    assert events == []


async def test_shared_backend_service_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact request Traefik Manager's /save endpoint receives to create the
    shared backend service — the "service with no router" resolution: a
    minimal router whose rule can never match real traffic (see the module
    docstring in orchestrator/adapters/proxy/traefik_manager.py)."""
    save_calls: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/routes":
            return httpx.Response(
                200,
                json={
                    "apps": [],
                    "middlewares": [],
                    "services": {"http": [], "tcp": [], "udp": []},  # not created yet
                },
            )
        if path == "/save":
            from urllib.parse import parse_qsl

            save_calls.append(dict(parse_qsl(request.read().decode())))
            return httpx.Response(200, json={"ok": True})
        if path == "/save-middleware":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    await adapter.ensure_maintenance_fallback(hostname="media.example.com", service_slug="media")

    assert len(save_calls) == 1
    call = save_calls[0]
    assert call["serviceName"] == SHARED_BACKEND_SERVICE_NAME
    assert call["targetIp"] == "10.0.0.5"
    assert call["targetPort"] == "8000"
    # Never matches real traffic: a reserved RFC 2606 .invalid hostname.
    assert ".invalid" in call["httpRule"]
    assert call["isEdit"] == "false"


async def test_shared_backend_service_is_not_recreated_when_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_calls: list[Any] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/routes":
            return httpx.Response(
                200,
                json={
                    "apps": [],
                    "middlewares": [],
                    "services": {"http": [SHARED_BACKEND_SERVICE_NAME], "tcp": [], "udp": []},
                },
            )
        if path == "/save":
            save_calls.append(request)
            return httpx.Response(200, json={"ok": True})
        if path == "/save-middleware":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    await adapter.ensure_maintenance_fallback(hostname="media.example.com", service_slug="media")

    assert save_calls == []


async def test_save_middleware_posts_form_with_fetch_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _router_calls(monkeypatch)
    adapter = TraefikManagerProxyAdapter(_settings())

    await adapter.ensure_maintenance_fallback(hostname="media.example.com", service_slug="media")

    assert len(state["save_middleware_calls"]) == 1
    body = state["save_middleware_calls"][0]
    assert "middlewareName=maintenance-media" in body
    assert "isMwEdit=false" in body
    assert "application/x-www-form-urlencoded" in state["save_middleware_content_type"]
    assert state["save_middleware_headers"]["x-requested-with"] == "fetch"


# ---------------------------------------------------------------------------
# General route CRUD — create_route/update_route/delete_route
#
# Unlike ensure/remove_maintenance_fallback above, these RAISE on failure —
# this IS the explicit action an admin just took (see ProxyAdapter's
# docstring and docs/proxy_hosts.md's "Live-managed proxy hosts" section).
# ---------------------------------------------------------------------------


async def test_create_route_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    save_calls: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/save":
            from urllib.parse import parse_qsl

            save_calls.append(dict(parse_qsl(request.read().decode())))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    ref = await adapter.create_route(hostname="app.example.com", upstream="http://10.0.0.9:8080")

    assert ref == route_name_for_hostname("app.example.com")
    assert len(save_calls) == 1
    call = save_calls[0]
    assert call["serviceName"] == ref
    assert call["targetIp"] == "10.0.0.9"
    assert call["targetPort"] == "8080"
    assert call["httpRule"] == "Host(`app.example.com`)"
    assert call["isEdit"] == "false"


async def test_create_route_raises_on_invalid_upstream() -> None:
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(ValueError):
        await adapter.create_route(hostname="app.example.com", upstream="not-a-url")


async def test_create_route_raises_on_unreachable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    from orchestrator.adapters.proxy.traefik_manager import TraefikManagerUnavailable

    with pytest.raises(TraefikManagerUnavailable):
        await adapter.create_route(hostname="app.example.com", upstream="http://10.0.0.9:80")


async def test_update_route_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    save_calls: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/save":
            from urllib.parse import parse_qsl

            save_calls.append(dict(parse_qsl(request.read().decode())))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())
    ref = route_name_for_hostname("app.example.com")

    await adapter.update_route(ref, hostname="new.example.com", upstream="http://10.0.0.5:9090")

    assert len(save_calls) == 1
    call = save_calls[0]
    assert call["serviceName"] == ref
    assert call["originalId"] == ref
    assert call["isEdit"] == "true"
    assert call["httpRule"] == "Host(`new.example.com`)"
    assert call["targetIp"] == "10.0.0.5"
    assert call["targetPort"] == "9090"


async def test_update_route_raises_on_invalid_upstream() -> None:
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(ValueError):
        await adapter.update_route("some-ref", hostname="app.example.com", upstream="")


async def test_delete_route_posts_to_delete_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/delete/orchestrator-app-example-com":
            delete_calls.append(request.url.path)
            assert request.headers.get("x-requested-with") == "fetch"
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    await adapter.delete_route("orchestrator-app-example-com")

    assert delete_calls == ["/delete/orchestrator-app-example-com"]


async def test_delete_route_raises_proxy_route_not_found_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False, "error": "not found"})

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    with pytest.raises(ProxyRouteNotFound):
        await adapter.delete_route("gone-ref")


async def test_delete_route_raises_on_unreachable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_transport(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())

    from orchestrator.adapters.proxy.traefik_manager import TraefikManagerUnavailable

    with pytest.raises(TraefikManagerUnavailable):
        await adapter.delete_route("some-ref")
