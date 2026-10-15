"""Traefik Manager client: auth header, success and failure paths."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.config import Settings
from orchestrator.registry.traefik_manager_client import (
    TraefikManagerClient,
    TraefikManagerNotConfigured,
    TraefikManagerNotFound,
    TraefikManagerUnavailable,
)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import orchestrator.registry.traefik_manager_client as tm_mod

    real_async_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_async_client):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tm_mod.httpx, "AsyncClient", _PatchedAsyncClient)


def _settings(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {
        "traefik_manager_url": "http://tm.test:8080",
        "traefik_manager_api_key": "secret-key",
    }
    base.update(kwargs)
    return Settings(**base)


def test_not_configured_raises_before_any_call() -> None:
    settings = Settings(traefik_manager_url="")
    with pytest.raises(TraefikManagerNotConfigured):
        TraefikManagerClient(settings)


async def test_get_routes_sends_api_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["api_key"] = request.headers.get("X-Api-Key")
        return httpx.Response(200, json={"apps": [], "middlewares": [], "services": {}})

    _patch_transport(monkeypatch, handle)
    client = TraefikManagerClient(_settings())
    body = await client.get_routes()
    assert seen["path"] == "/api/routes"
    assert seen["api_key"] == "secret-key"
    assert body == {"apps": [], "middlewares": [], "services": {}}


async def test_connection_failure_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_transport(monkeypatch, handle)
    client = TraefikManagerClient(_settings())
    with pytest.raises(TraefikManagerUnavailable):
        await client.get_routes()


async def test_timeout_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    _patch_transport(monkeypatch, handle)
    client = TraefikManagerClient(_settings(traefik_manager_timeout_s=0.01))
    with pytest.raises(TraefikManagerUnavailable):
        await client.get_routes()


async def test_401_raises_unavailable_not_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": "Not authenticated"})

    _patch_transport(monkeypatch, handle)
    client = TraefikManagerClient(_settings())
    with pytest.raises(TraefikManagerUnavailable):
        await client.get_routes()


async def test_404_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False, "error": "not found"})

    _patch_transport(monkeypatch, handle)
    client = TraefikManagerClient(_settings())
    with pytest.raises(TraefikManagerNotFound):
        await client.get_route_raw("missing-route")


async def test_save_middleware_posts_form_with_fetch_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["requested_with"] = request.headers.get("X-Requested-With")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    _patch_transport(monkeypatch, handle)
    client = TraefikManagerClient(_settings())
    await client.save_middleware("maintenance-media", "errors:\n  status: []\n", is_edit=True)

    assert seen["path"] == "/save-middleware"
    assert "application/x-www-form-urlencoded" in seen["content_type"]
    assert seen["requested_with"] == "fetch"
    assert "middlewareName=maintenance-media" in seen["body"]
    assert "isMwEdit=true" in seen["body"]


async def test_save_route_raw_sends_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True})

    _patch_transport(monkeypatch, handle)
    client = TraefikManagerClient(_settings())
    await client.save_route_raw("my-router", "http:\n  routers: {}\n")

    assert "application/json" in seen["content_type"]
    assert json.loads(seen["body"]) == {"content": "http:\n  routers: {}\n"}


async def test_get_route_raw_returns_parsed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/routes/my-router/raw"
        return httpx.Response(200, json={"raw": "http:\n  routers: {}\n", "configFile": "a.yml"})

    _patch_transport(monkeypatch, handle)
    client = TraefikManagerClient(_settings())
    body = await client.get_route_raw("my-router")
    assert body["raw"] == "http:\n  routers: {}\n"
