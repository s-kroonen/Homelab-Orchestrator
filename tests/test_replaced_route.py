"""A host whose own route is gone while a hand-written one serves its hostname (a failover
that replaced it): never "re-created" next to it, and Adopt / Resync point the host there."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from orchestrator.adapters.proxy.base import RouteInfo
from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter
from orchestrator.db.models import ProxyHost
from orchestrator.registry.proxy_push import EXTERNAL, PENDING_CREATE, evaluate, stale_ref_route
from tests.test_proxy_adapter_traefik_manager import _settings

HOST = "panel.example.lan"
OURS = "orchestrator-panel-example-lan"


def _host() -> ProxyHost:
    return ProxyHost(
        hostname=HOST,
        upstream="http://10.0.0.14:80",
        extra={"traefik_manager_route_id": OURS},
        pushed_sig="pushed-before",
    )


def _theirs() -> RouteInfo:
    return RouteInfo(
        name="panel",
        route_id="panel.yml::panel",
        hostnames={HOST},
        upstream="http://10.0.0.14:80",
        service="panel",
        config_file="panel.yml",
    )


def test_a_replaced_route_is_external_not_re_created() -> None:
    proxy = TraefikManagerProxyAdapter(_settings())
    state = evaluate(proxy, _host(), [_theirs()], None)
    assert state.state == EXTERNAL and state.actions == []
    assert state.changes[0].startswith("served by the route panel")
    assert stale_ref_route(proxy, _host(), [_theirs()]) is not None
    # nothing serves it at all: still ours to re-create
    gone = evaluate(proxy, _host(), [], None)
    assert gone.state == PENDING_CREATE and "create" in gone.actions


def test_adopt_points_the_host_at_the_hand_written_route(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlmodel import Session, select

    from orchestrator.db.session import get_engine
    from tests.test_proxy_hosts import _audit_actions_all, _patch_tm_transport, _tm_client

    client = _tm_client(
        monkeypatch, TRAEFIK_MANAGER_URL="http://tm.test:8080", TRAEFIK_MANAGER_API_KEY="key"
    )
    apps: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/routes":
            return httpx.Response(200, json={"apps": apps, "middlewares": [], "configErrors": []})
        return httpx.Response(404, json={})

    _patch_tm_transport(monkeypatch, handle)
    made = client.post(
        "/api/proxy-hosts",
        json={
            "hostname": HOST,
            "upstream": "http://10.0.0.14:80",
            "service_id": service.id,
            "entry_points": ["websecure"],
        },
    )
    assert made.status_code == 201, made.text
    host_id = made.json()["id"]
    with Session(get_engine()) as s:  # it was pushed once, then replaced by hand
        ph = s.exec(select(ProxyHost).where(ProxyHost.hostname == HOST)).one()
        ph.pushed_sig = "pushed-before"
        s.add(ph)
        s.commit()
    apps.append(
        {
            "id": "panel.yml::panel",
            "name": "panel",
            "protocol": "http",
            "rule": f"Host(`{HOST}`)",
            "service_name": "panel",
            "configFile": "panel.yml",
        }
    )
    row = next(h for h in client.get("/api/proxy-hosts").json() if h["id"] == host_id)
    assert row["sync_state"] == "external" and row["adoptable"] is True

    done = client.post(f"/api/proxy-hosts/{host_id}/adopt-route")
    assert done.status_code == 200, done.text
    assert done.json()["route"] == "panel.yml::panel"
    assert "proxy_host.route_adopted" in _audit_actions_all()
    row = next(h for h in client.get("/api/proxy-hosts").json() if h["id"] == host_id)
    assert row["sync_state"] == "external" and row["adoptable"] is False
    assert client.post(f"/api/proxy-hosts/{host_id}/adopt-route").status_code == 409
