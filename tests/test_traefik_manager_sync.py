"""Maintenance-page fallback middleware: shared backend creation, per-slug
middleware, and — the one hard requirement — the raw-YAML read-modify-write
injection that must survive every other field and middleware untouched and
be idempotent on re-run.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from orchestrator.config import Settings
from orchestrator.registry.traefik_manager_sync import (
    SHARED_BACKEND_SERVICE_NAME,
    maintenance_middleware_name,
    sync_maintenance_middleware,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import orchestrator.registry.traefik_manager_client as tm_mod

    class _Patched(_REAL_ASYNC_CLIENT):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tm_mod.httpx, "AsyncClient", _Patched)


def _settings(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {
        "traefik_manager_url": "http://tm.test:8080",
        "traefik_manager_api_key": "key",
        "orchestrator_public_url": "http://10.0.0.5:8000",
    }
    base.update(kwargs)
    return Settings(**base)


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
    """Build a stateful handler simulating just enough of Traefik Manager
    for the injection tests: GET /api/routes (one matching app), GET/POST
    the raw YAML for it, and GET /api/routes for the shared-backend-service
    existence check."""
    state = {"raw": RAW_ROUTE_WITH_EXTRAS, "saved_raw_calls": [], "save_middleware_calls": []}

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
                import json as _json

                content = _json.loads(request.read())["content"]
                state["raw"] = content
                state["saved_raw_calls"].append(content)
                return httpx.Response(200, json={"ok": True})
        if path == "/save-middleware":
            state["save_middleware_calls"].append(request.read().decode())
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)
    return state


async def test_injects_middleware_preserving_every_other_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _router_calls(monkeypatch)

    events = await sync_maintenance_middleware(
        _settings(), hostname="media.example.com", service_slug="media"
    )

    mw_name = maintenance_middleware_name("media")
    assert any(e.action == "middleware.injected" for e in events)
    assert len(state["saved_raw_calls"]) == 1
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


async def test_injection_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _router_calls(monkeypatch)

    await sync_maintenance_middleware(
        _settings(), hostname="media.example.com", service_slug="media"
    )
    first_raw = state["raw"]

    events = await sync_maintenance_middleware(
        _settings(), hostname="media.example.com", service_slug="media"
    )

    # No second raw-YAML write — the middleware name was already present.
    assert len(state["saved_raw_calls"]) == 1
    assert state["raw"] == first_raw
    assert not any(e.action == "middleware.injected" for e in events)
    mw_name = maintenance_middleware_name("media")
    assert state["raw"].count(mw_name) == 1  # never duplicated


async def test_no_op_when_traefik_manager_url_unset() -> None:
    events = await sync_maintenance_middleware(
        Settings(traefik_manager_url="", orchestrator_public_url="http://10.0.0.5:8000"),
        hostname="media.example.com",
        service_slug="media",
    )
    assert events == []


async def test_no_op_when_orchestrator_public_url_unset() -> None:
    events = await sync_maintenance_middleware(
        Settings(
            traefik_manager_url="http://tm.test:8080",
            traefik_manager_api_key="key",
            orchestrator_public_url="",
        ),
        hostname="media.example.com",
        service_slug="media",
    )
    assert events == []


async def test_unreachable_traefik_manager_is_caught_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_transport(monkeypatch, handle)

    # Must not raise — this is a side effect of an ordinary ProxyHost CRUD
    # request, not an explicit admin-triggered write like Resync.
    events = await sync_maintenance_middleware(
        _settings(), hostname="media.example.com", service_slug="media"
    )
    assert events == []


async def test_rename_moves_injection_from_old_hostname_to_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {
        "old": RAW_ROUTE_WITH_EXTRAS,
        "new": RAW_ROUTE_WITH_EXTRAS.replace("media.example.com", "new.example.com").replace(
            "media-router", "new-router"
        ),
    }
    mw_name = maintenance_middleware_name("media")
    # Pretend the old route already has the middleware injected.
    state["old"] = state["old"].replace(
        "      middlewares:\n        - chain-no-auth\n",
        f"      middlewares:\n        - chain-no-auth\n        - {mw_name}\n",
    )

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
                        },
                        {
                            "id": "new-router",
                            "name": "new-router",
                            "protocol": "http",
                            "rule": "Host(`new.example.com`)",
                        },
                    ],
                    "middlewares": [{"name": mw_name}],
                    "services": {"http": [SHARED_BACKEND_SERVICE_NAME], "tcp": [], "udp": []},
                },
            )
        if path == "/api/routes/media-router/raw" and request.method == "GET":
            return httpx.Response(200, json={"raw": state["old"]})
        if path == "/api/routes/media-router/raw" and request.method == "POST":
            import json as _json

            state["old"] = _json.loads(request.read())["content"]
            return httpx.Response(200, json={"ok": True})
        if path == "/api/routes/new-router/raw" and request.method == "GET":
            return httpx.Response(200, json={"raw": state["new"]})
        if path == "/api/routes/new-router/raw" and request.method == "POST":
            import json as _json

            state["new"] = _json.loads(request.read())["content"]
            return httpx.Response(200, json={"ok": True})
        if path == "/save-middleware":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)

    await sync_maintenance_middleware(
        _settings(),
        hostname="new.example.com",
        service_slug="media",
        old_hostname="media.example.com",
        old_service_slug="media",
    )

    assert mw_name not in state["old"]
    assert mw_name in state["new"]


async def test_relink_to_different_service_uses_new_slugs_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_mw = maintenance_middleware_name("media")
    new_mw = maintenance_middleware_name("other")
    raw = RAW_ROUTE_WITH_EXTRAS.replace(
        "      middlewares:\n        - chain-no-auth\n",
        f"      middlewares:\n        - chain-no-auth\n        - {old_mw}\n",
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
                    "middlewares": [{"name": old_mw}],
                    "services": {"http": [SHARED_BACKEND_SERVICE_NAME], "tcp": [], "udp": []},
                },
            )
        if path == "/api/routes/media-router/raw" and request.method == "GET":
            return httpx.Response(200, json={"raw": state["raw"]})
        if path == "/api/routes/media-router/raw" and request.method == "POST":
            import json as _json

            state["raw"] = _json.loads(request.read())["content"]
            return httpx.Response(200, json={"ok": True})
        if path == "/save-middleware":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)

    await sync_maintenance_middleware(
        _settings(),
        hostname="media.example.com",
        service_slug="other",
        old_hostname="media.example.com",
        old_service_slug="media",
    )

    assert old_mw not in state["raw"]
    assert new_mw in state["raw"]


async def test_unlink_removes_injection_leaves_middleware_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mw_name = maintenance_middleware_name("media")
    raw = RAW_ROUTE_WITH_EXTRAS.replace(
        "      middlewares:\n        - chain-no-auth\n",
        f"      middlewares:\n        - chain-no-auth\n        - {mw_name}\n",
    )
    state = {"raw": raw, "delete_calls": 0}

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
            import json as _json

            state["raw"] = _json.loads(request.read())["content"]
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_transport(monkeypatch, handle)

    events = await sync_maintenance_middleware(
        _settings(),
        hostname=None,
        service_slug=None,
        old_hostname="media.example.com",
        old_service_slug="media",
    )

    assert mw_name not in state["raw"]
    assert "chain-no-auth" in state["raw"]  # untouched
    assert any(e.action == "middleware.removed" for e in events)
    # The middleware DEFINITION itself is never deleted by this module.
    assert not any("delete-middleware" in e.action for e in events)


async def test_shared_backend_service_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact request Traefik Manager's /save endpoint receives to create the
    shared backend service — the "service with no router" resolution: a
    minimal router whose rule can never match real traffic (see module
    docstring in traefik_manager_sync.py)."""
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

    await sync_maintenance_middleware(
        _settings(), hostname="media.example.com", service_slug="media"
    )

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

    await sync_maintenance_middleware(
        _settings(), hostname="media.example.com", service_slug="media"
    )

    assert save_calls == []
