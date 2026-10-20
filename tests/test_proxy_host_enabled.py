"""Enable / disable a proxy host without deleting it.

Adapter: the Traefik Manager toggle request (``POST /api/routes/<id>/toggle``,
``{"enable": bool}``, API key + ``X-Requested-With: fetch``), the companion
``-redirect`` router, rollback when the second toggle fails, and the dry-run
adapter. API: Traefik Manager first (a failure leaves the DB alone), local-only
hosts just store, editing a disabled live host is refused, and the list /
Resync show "disabled" instead of an error or drift.

``ToggleTM`` extends the simulated Traefik Manager of
``test_proxy_entrypoints_api`` with the toggle endpoint, storing a disabled
route the way Traefik Manager does: out of the config (so Traefik's runtime
routers no longer list it) and listed by ``/api/routes`` with
``"enabled": false``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlmodel import Session, select

from orchestrator.adapters.proxy.base import RouteInfo
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import (
    TraefikManagerProxyAdapter,
    _routes_to_route_info,
    redirect_route_id,
)
from orchestrator.db.models import AuditEntry, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.registry.loader import read_yaml_file
from orchestrator.registry.schema import ProxyHostSpec
from tests.test_proxy_entrypoints_adapter import _patch, _settings
from tests.test_proxy_entrypoints_api import ROUTE_REF_KEY, FakeTM, _body, _client

# ---------------------------------------------------------------------------
# Simulated Traefik Manager with the toggle endpoint
# ---------------------------------------------------------------------------


class ToggleTM(FakeTM):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.disabled: dict[str, dict[str, Any]] = {}
        self.toggles: list[tuple[str, bool]] = []
        self.toggle_requests: list[httpx.Request] = []
        self.fail_toggle: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/routes/") and path.endswith("/toggle"):
            rid = path.removeprefix("/api/routes/").removesuffix("/toggle")
            enable = json.loads(request.read())["enable"]
            self.toggle_requests.append(request)
            if rid in self.fail_toggle:
                return httpx.Response(500, json={"ok": False, "message": "disk full"})
            self.toggles.append((rid, enable))
            if enable and rid in self.disabled:
                self.routes[rid] = self.disabled.pop(rid)
            elif not enable and rid in self.routes:
                self.disabled[rid] = self.routes.pop(rid)
            return httpx.Response(200, json={"ok": True})
        if path == "/api/routes":
            response = super().__call__(request)
            body = response.json()
            for app in body["apps"]:
                app["enabled"] = True
            for name, r in self.disabled.items():
                body["apps"].append(
                    {
                        "id": name,
                        "name": name,
                        "protocol": "http",
                        "rule": r["rule"],
                        "target": r["upstream"],
                        "entryPoints": r["eps"],
                        "enabled": False,
                    }
                )
            return httpx.Response(200, json=body)
        return super().__call__(request)


def _audit(prefix: str) -> list[AuditEntry]:
    with Session(get_engine()) as s:
        return list(
            s.exec(
                select(AuditEntry).where(AuditEntry.action.startswith(prefix))  # type: ignore[attr-defined]
            ).all()
        )


def _row(ph_id: int) -> ProxyHost:
    with Session(get_engine()) as s:
        row = s.get(ProxyHost, ph_id)
        assert row is not None
        return row


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class _Rec:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = fail or set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        rid = request.url.path.removeprefix("/api/routes/").removesuffix("/toggle")
        if rid in self.fail:
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    def calls(self) -> list[tuple[str, bool]]:
        return [(r.url.path, json.loads(r.read())["enable"]) for r in self.requests]


async def test_toggle_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Rec()
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings(traefik_manager_api_key="secret"))
    await adapter.set_route_enabled("orchestrator-app-example-com", False)
    (req,) = rec.requests
    assert req.method == "POST"
    assert req.url.path == "/api/routes/orchestrator-app-example-com/toggle"
    assert json.loads(req.read()) == {"enable": False}
    assert req.headers["X-Api-Key"] == "secret"
    assert req.headers["X-Requested-With"] == "fetch"
    assert req.headers["content-type"] == "application/json"


async def test_toggle_takes_the_redirect_companion_along_in_a_safe_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Rec()
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    ref = "orchestrator-app-example-com"
    await adapter.set_route_enabled(ref, False, with_redirect=True)
    await adapter.set_route_enabled(ref, True, with_redirect=True)
    assert rec.calls() == [
        # Disable: the plain-HTTP redirect first, never pointing at nothing.
        (f"/api/routes/{redirect_route_id(ref)}/toggle", False),
        (f"/api/routes/{ref}/toggle", False),
        # Enable: the route first, then its redirect.
        (f"/api/routes/{ref}/toggle", True),
        (f"/api/routes/{redirect_route_id(ref)}/toggle", True),
    ]


async def test_a_failed_second_toggle_undoes_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = "orchestrator-app-example-com"
    rec = _Rec(fail={ref})
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(Exception, match="500"):
        await adapter.set_route_enabled(ref, False, with_redirect=True)
    assert rec.calls() == [
        (f"/api/routes/{redirect_route_id(ref)}/toggle", False),
        (f"/api/routes/{ref}/toggle", False),
        (f"/api/routes/{redirect_route_id(ref)}/toggle", True),  # put back
    ]


def test_list_routes_reports_a_disabled_route() -> None:
    infos = _routes_to_route_info(
        [
            {"name": "a", "rule": "Host(`a.example.com`)", "protocol": "http"},
            {"name": "b", "rule": "Host(`b.example.com`)", "protocol": "http", "enabled": False},
        ]
    )
    assert [(i.name, i.enabled) for i in infos] == [("a", True), ("b", False)]


async def test_dry_run_records_toggles_and_parks_the_runtime_router() -> None:
    adapter = DryRunProxyAdapter()
    ref = await adapter.create_route(
        hostname="a.example.com", upstream="http://1.2.3.4:80", entry_points=["websecure"]
    )
    await adapter.set_route_enabled(ref, False, with_redirect=True)
    assert await adapter.list_runtime_routers() == []
    await adapter.set_route_enabled(ref, True)
    assert [r.name for r in await adapter.list_runtime_routers() or []] == [f"{ref}@file"]
    assert adapter.toggles == [(ref, False, True), (ref, True, False)]


async def test_base_adapter_says_it_cannot_toggle() -> None:
    from orchestrator.adapters.proxy.base import ProxyAdapter

    class Minimal(DryRunProxyAdapter):
        set_route_enabled = ProxyAdapter.set_route_enabled  # type: ignore[assignment]

    with pytest.raises(NotImplementedError, match="does not support"):
        await Minimal().set_route_enabled("x", False)


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


def test_spec_defaults_to_enabled() -> None:
    assert ProxyHostSpec(hostname="a.example.com").enabled is True
    assert ProxyHostSpec(hostname="a.example.com", enabled=False).enabled is False


def test_enabled_round_trips_through_save_and_reset(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from fastapi.testclient import TestClient

    from orchestrator.db import session as session_module
    from orchestrator.web.app import create_app
    from tests.test_proxy_hosts import _login_as_first_admin

    session_module.create_all_for_tests()
    with TestClient(create_app()) as client:
        _login_as_first_admin(client, monkeypatch)
        ph = client.post(
            "/api/proxy-hosts",
            json={"hostname": "a.example.com", "service_id": service.id, "router_provider": "npm"},
        ).json()
        assert (
            client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False}).status_code
            == 200
        )
        assert client.post("/api/registry/save").status_code == 200
        parsed, _ = read_yaml_file(tmp_path / "services.yaml")
        assert parsed.services[0].proxy_hosts[0].enabled is False
        assert client.post("/api/registry/reset").status_code == 200
        (row,) = client.get("/api/proxy-hosts").json()
        assert row["enabled"] is False and row["live_status"] == "disabled"


# ---------------------------------------------------------------------------
# API against the simulated Traefik Manager
# ---------------------------------------------------------------------------


def _live_host(client: Any, service: Service, hostname: str = "app.example.com") -> dict[str, Any]:
    r = client.post("/api/proxy-hosts", json=_body(service, hostname=hostname))
    assert r.status_code == 201, r.text
    return r.json()


def test_disable_toggles_traefik_manager_first_then_stores(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    ref = ph["extra"][ROUTE_REF_KEY]

    r = client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert tm.toggles == [(ref, False)]
    assert body["enabled"] is False
    # Absent from Traefik's runtime routers on purpose: not an error.
    assert body["live_status"] == "disabled"
    assert body["traefik"]["state"] == "disabled"
    assert _row(ph["id"]).enabled is False
    (entry,) = _audit("proxy_host.disabled")
    assert entry.details["route_ref"] == ref and entry.details["live"] is True

    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["traefik"]["state"] == "disabled"

    r = client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert tm.toggles[-1] == (ref, True)
    assert r.json()["traefik"]["state"] == "ok"
    assert r.json()["live_status"] == "matched"
    assert len(_audit("proxy_host.enabled")) == 1


def test_a_failed_toggle_leaves_the_db_alone(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    tm.fail_toggle.add(ph["extra"][ROUTE_REF_KEY])

    r = client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    assert r.status_code == 502
    assert "could not disable the route on Traefik Manager" in r.json()["detail"]
    assert _row(ph["id"]).enabled is True
    assert _audit("proxy_host.disabled") == []


def test_redirect_companion_is_toggled_too(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    ref = ph["extra"][ROUTE_REF_KEY]
    with Session(get_engine()) as s:
        row = s.get(ProxyHost, ph["id"])
        assert row is not None
        row.route_options = {"http_redirect": True}
        s.add(row)
        s.commit()
    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    assert [rid for rid, _ in tm.toggles] == [redirect_route_id(ref), ref]


def test_local_only_host_just_stores(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    r = client.post(
        "/api/proxy-hosts", json=_body(service, router_provider="pangolin", upstream="")
    )
    ph = r.json()
    r = client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert tm.toggle_requests == []
    assert _row(ph["id"]).enabled is False
    (entry,) = _audit("proxy_host.disabled")
    assert entry.details["live"] is False


def test_editing_a_disabled_live_host_is_refused(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    saves_before = len(tm.saves)

    r = client.put(
        f"/api/proxy-hosts/{ph['id']}", json=_body(service, upstream="http://10.0.0.7:80")
    )
    assert r.status_code == 409
    assert "Enable it first" in r.json()["detail"]["message"]
    # Nothing was written anywhere: no /save, the route is still parked.
    assert len(tm.saves) == saves_before
    assert ph["extra"][ROUTE_REF_KEY] in tm.disabled
    assert _row(ph["id"]).upstream == "http://10.0.0.9:8080"


def test_editing_a_disabled_local_host_is_allowed_and_keeps_it_disabled(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = client.post(
        "/api/proxy-hosts", json=_body(service, router_provider="npm", upstream="")
    ).json()
    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    r = client.put(
        f"/api/proxy-hosts/{ph['id']}", json=_body(service, router_provider="npm", upstream="")
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    # Turning it into a live route while disabled would create an ENABLED
    # route on Traefik Manager: refused too.
    r = client.put(f"/api/proxy-hosts/{ph['id']}", json=_body(service))
    assert r.status_code == 409


def test_resync_does_not_flag_a_disabled_host(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["discovered"] == []  # its parked route is still recognised as ours
    (updated,) = body["updated"]
    assert updated["traefik"]["state"] == "disabled"
    assert _row(ph["id"]).enabled is False


def test_route_disabled_on_tm_but_enabled_here_is_explained(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    ref = ph["extra"][ROUTE_REF_KEY]
    tm.disabled[ref] = tm.routes.pop(ref)  # someone flipped it in Traefik Manager
    (row,) = client.get("/api/proxy-hosts").json()
    assert row["traefik"]["state"] == "not_loaded"
    assert "Traefik Manager has this route disabled" in row["traefik"]["message"]


def test_route_info_enabled_defaults_true() -> None:
    assert RouteInfo(name="x", hostnames=set(), upstream=None).enabled is True
