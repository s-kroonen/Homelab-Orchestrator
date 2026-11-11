"""A Traefik Manager that cannot list its routes is "unknown", never "the route is gone".

Incident (2026-11): one dynamic config file the manager's user could not open made
``GET /api/routes`` answer 500 for every host, and the proxy source still read "0 routes".
A file that does not parse is dropped from the list silently (it is only named in
``configErrors``), which made a hand-written host look removed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter
from orchestrator.db.models import ProxyHost
from orchestrator.registry.proxy_push import PENDING_UPDATE, evaluate
from tests.test_proxy_adapter_traefik_manager import _patch_transport, _settings

PROBE = "__orchestrator_read_probe__"


def _tm(*, routes: httpx.Response, unreadable: set[str] | None = None) -> Any:
    files = ["app.yml", "game.yml"]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/routes":
            return routes
        if path == "/api/configs":
            return httpx.Response(
                200, json={"files": [{"label": f, "path": f"/dyn/{f}"} for f in files]}
            )
        for f in files:
            if path == f"/api/routes/{f}::{PROBE}/raw":
                if f in (unreadable or set()):
                    return httpx.Response(500, text="<h1>Internal Server Error</h1>")
                return httpx.Response(404, json={"error": "Route not found"})
        return httpx.Response(404, json={})

    return handle


async def test_an_unreadable_file_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(
        monkeypatch,
        _tm(routes=httpx.Response(500, text="boom"), unreadable={"game.yml"}),
    )
    adapter = TraefikManagerProxyAdapter(_settings())
    assert await adapter.list_routes() is None
    assert adapter.read_error is not None and "game.yml" in adapter.read_error
    assert "app.yml" not in adapter.read_error
    files = await adapter.unreadable_files()
    assert [f["file"] for f in files or []] == ["game.yml"]


async def test_a_file_that_does_not_parse_makes_the_list_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "apps": [{"id": "app", "name": "app", "protocol": "http", "rule": "Host(`a.example.lan`)"}],
        "middlewares": [],
        "configErrors": [{"file": "game.yml", "error": "mapping values are not allowed here"}],
    }
    _patch_transport(monkeypatch, _tm(routes=httpx.Response(200, json=body)))
    adapter = TraefikManagerProxyAdapter(_settings())
    assert await adapter.list_routes() is None
    assert "game.yml" in (adapter.read_error or "")

    # every host is then "not compared", naming why - never "the route is gone"
    ph = ProxyHost(
        hostname="game.example.lan",
        upstream="http://10.0.0.14:80",
        extra={"traefik_manager_route_id": "orchestrator-game-example-lan"},
        pushed_sig="x",
    )
    state = evaluate(adapter, ph, None, "game")
    assert state.state == PENDING_UPDATE
    assert "could not be read" in state.changes[0] and "game.yml" in state.changes[0]


async def test_a_good_read_clears_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"apps": [], "middlewares": [], "configErrors": []}
    _patch_transport(monkeypatch, _tm(routes=httpx.Response(200, json=body)))
    adapter = TraefikManagerProxyAdapter(_settings())
    adapter.read_error = "old"
    assert await adapter.list_routes() == []
    assert adapter.read_error is None
