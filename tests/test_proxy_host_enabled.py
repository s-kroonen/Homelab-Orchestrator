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
from tests.push_helpers import actions, failures, push, sync_state
from tests.sim_tm import SimTM
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


async def test_toggle_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    sim = SimTM(multi=False)
    sim.add("dynamic.yml", "orchestrator-app-example-com", "app.example.com")
    _patch(monkeypatch, sim)
    adapter = TraefikManagerProxyAdapter(_settings(traefik_manager_api_key="secret"))
    await adapter.set_route_enabled("orchestrator-app-example-com", False)
    (req,) = sim.toggle_requests
    assert req.method == "POST"
    assert req.url.path == "/api/routes/orchestrator-app-example-com/toggle"
    assert json.loads(req.read()) == {"enable": False}
    assert req.headers["X-Api-Key"] == "secret"
    assert req.headers["X-Requested-With"] == "fetch"
    assert req.headers["content-type"] == "application/json"
    assert sim.is_enabled("orchestrator-app-example-com") is False


async def test_toggle_takes_the_redirect_companion_along_in_a_safe_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM(multi=False)
    ref = "orchestrator-app-example-com"
    sim.add("dynamic.yml", ref, "app.example.com")
    sim.add("dynamic.yml", redirect_route_id(ref), "app.example.com", eps=("web",))
    _patch(monkeypatch, sim)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.set_route_enabled(ref, False, with_redirect=True)
    await adapter.set_route_enabled(ref, True, with_redirect=True)
    assert [(rid, en) for k, rid, en in sim.requests if k == "toggle"] == [
        # Disable: the plain-HTTP redirect first, never pointing at nothing.
        (redirect_route_id(ref), False),
        (ref, False),
        # Enable: the route first, then its redirect.
        (ref, True),
        (redirect_route_id(ref), True),
    ]


async def test_a_failed_second_toggle_undoes_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = "orchestrator-app-example-com"
    sim = SimTM(multi=False)
    sim.add("dynamic.yml", ref, "app.example.com")
    sim.add("dynamic.yml", redirect_route_id(ref), "app.example.com", eps=("web",))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/routes/{ref}/toggle":
            return httpx.Response(500, json={"ok": False})
        return sim(request)

    _patch(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(Exception, match="500"):
        await adapter.set_route_enabled(ref, False, with_redirect=True)
    # The redirect that was switched off first is put back.
    assert [(rid, en) for k, rid, en in sim.requests if k == "toggle"] == [
        (redirect_route_id(ref), False),
        (redirect_route_id(ref), True),
    ]
    assert sim.is_enabled(redirect_route_id(ref)) is True


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


def test_redirect_companion_is_toggled_too(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    ref = "orchestrator-app-example-com"
    tm.seed(ref, "app.example.com", ["websecure"])
    tm.seed(redirect_route_id(ref), "app.example.com", ["web"])
    client = _client(monkeypatch, tm)
    with Session(get_engine()) as s:  # a host recorded earlier, whose pair already exists
        row = ProxyHost(
            hostname="app.example.com",
            upstream="http://1.2.3.4:80",
            service_id=service.id,
            entry_points=["websecure"],
            route_options={"http_redirect": True},
            extra={ROUTE_REF_KEY: ref},
        )
        s.add(row)
        s.commit()
        s.refresh(row)
    ph = client.get(f"/api/proxy-hosts/{row.id}").json()
    assert ph["sync_state"] == "in_sync"  # what Traefik Manager has already matches
    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    push(client)
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
    assert r.json()["sync_state"] == "local"
    push(client)
    assert tm.toggle_requests == []
    assert _row(ph["id"]).enabled is False
    assert len(_audit("proxy_host.disabled")) == 1


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
    # Turning it into a live route while disabled is fine too: the push
    # creates the route and switches it off again straight away.
    r = client.put(f"/api/proxy-hosts/{ph['id']}", json=_body(service))
    assert r.status_code == 200, r.text
    assert r.json()["sync_state"] == "pending_create"
    result = push(client)
    assert actions(result) == [("app.example.com", "create"), ("app.example.com", "disable")]
    assert r.json()["extra"][ROUTE_REF_KEY] in tm.disabled
    assert tm.routes == {}


def test_resync_does_not_flag_a_disabled_host(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    push(client)
    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    push(client)
    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["discovered"] == []  # its parked route is still recognised as ours
    (updated,) = body["updated"]
    assert updated["traefik"]["state"] == "disabled"
    assert _row(ph["id"]).enabled is False


def test_route_info_enabled_defaults_true() -> None:
    assert RouteInfo(name="x", hostnames=set(), upstream=None).enabled is True


# ---------------------------------------------------------------------------
# State kept in step with Traefik Manager (it is the source of truth)
# ---------------------------------------------------------------------------


def _adopted_host(service: Service, hostname: str = "pbs.example.com") -> int:
    """A host recorded WITHOUT a Traefik Manager route reference, the way a
    Resync "Discovered -> Add" leaves it."""
    with Session(get_engine()) as s:
        ph = ProxyHost(
            hostname=hostname,
            upstream="http://10.0.0.9:8006",
            service_id=service.id,
            entry_points=["websecure"],
        )
        s.add(ph)
        s.commit()
        s.refresh(ph)
        assert ph.id is not None
        return ph.id


def test_toggle_reaches_tm_for_a_host_without_a_stored_route_reference(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the switch only changed the local flag for adopted hosts, so
    Traefik Manager never saw the disable. Now the push switches every route
    serving the hostname - and leaves their content alone."""
    fake = ToggleTM()
    fake.seed("hand-made", "pbs.example.com", ["websecure"])
    fake.seed("hand-made-redirect", "pbs.example.com", ["web"])  # a separate redirect router
    client = _client(monkeypatch, fake)
    pid = _adopted_host(service)
    assert sync_state(client, pid) == "external"

    r = client.post(f"/api/proxy-hosts/{pid}/enabled", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert fake.toggles == []
    push(client)
    assert sorted(t for t in fake.toggles) == [("hand-made", False), ("hand-made-redirect", False)]
    assert fake.saves == []  # never overwritten
    assert _row(pid).enabled is False
    body = client.get(f"/api/proxy-hosts/{pid}").json()
    assert body["tm_state"] == "disabled" and body["enabled"] is False

    fake.toggles.clear()
    assert client.post(f"/api/proxy-hosts/{pid}/enabled", json={"enabled": True}).status_code == 200
    push(client)
    assert sorted(t for t in fake.toggles) == [("hand-made", True), ("hand-made-redirect", True)]


def test_list_shows_traefik_managers_state_and_a_mismatch(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ToggleTM()
    fake.seed("hand-made", "pbs.example.com", ["websecure"])
    client = _client(monkeypatch, fake)
    pid = _adopted_host(service)

    row = next(h for h in client.get("/api/proxy-hosts").json() if h["id"] == pid)
    assert row["enabled"] is True and row["tm_state"] == "enabled"
    assert row["sync_state"] == "external"

    # Switched off in Traefik Manager by hand: the orchestrator still says "on".
    fake.disabled["hand-made"] = fake.routes.pop("hand-made")
    row = next(h for h in client.get("/api/proxy-hosts").json() if h["id"] == pid)
    assert row["enabled"] is True and row["tm_state"] == "disabled"
    assert row["sync_state"] == "external" and row["sync_pending"] is True
    assert any("enabled state differs" in c for c in row["sync_changes"])


def test_partial_state_when_only_some_routes_are_off(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ToggleTM()
    fake.seed("main", "pbs.example.com", ["websecure"])
    fake.seed("redir", "pbs.example.com", ["web"])
    fake.disabled["redir"] = fake.routes.pop("redir")
    client = _client(monkeypatch, fake)
    pid = _adopted_host(service)
    row = next(h for h in client.get("/api/proxy-hosts").json() if h["id"] == pid)
    assert row["tm_state"] == "partial"


def test_resync_adopts_traefik_managers_disabled_state(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ToggleTM()
    fake.seed("hand-made", "pbs.example.com", ["websecure"])
    client = _client(monkeypatch, fake)
    pid = _adopted_host(service)
    fake.disabled["hand-made"] = fake.routes.pop("hand-made")  # disabled in TM

    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text
    assert _row(pid).enabled is False
    upd = next(u for u in r.json()["updated"] if u["hostname"] == "pbs.example.com")
    assert upd["enabled_changed"] is True

    # ...and back on again when TM has it enabled.
    fake.routes["hand-made"] = fake.disabled.pop("hand-made")
    assert client.post("/api/proxy-hosts/resync").status_code == 200
    assert _row(pid).enabled is True


def test_edit_page_has_the_enable_switch(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ToggleTM()
    fake.seed("hand-made", "pbs.example.com", ["websecure"])
    client = _client(monkeypatch, fake)
    pid = _adopted_host(service)
    # The on/off switch sits in every row of the list.
    js = client.get("/static/proxy_hosts.js").text
    assert 'class: "switch", role: "switch"' in js and "/enabled" in js
    assert '"enabled": true' in client.get("/proxy-hosts").text
    client.post(f"/api/proxy-hosts/{pid}/enabled", json={"enabled": False})
    assert '"enabled": false' in client.get("/proxy-hosts").text


def test_link_buttons_keep_their_colour_when_visited() -> None:
    """Regression: `a:visited` outranked `a.btn`, turning a visited "Add proxy
    host" button blue on blue (invisible text)."""
    from pathlib import Path

    css = (
        Path(__file__).parent.parent / "src" / "orchestrator" / "web" / "static" / "app.css"
    ).read_text(encoding="utf-8")
    # The generic link colour never applies to a button-link...
    assert "a:not(.btn), a:not(.btn):visited { color: var(--link)" in css
    # ...and button colours are pinned in every link state.
    assert "a.btn:visited" in css and "a.btn.secondary:visited" in css
    assert "color: #fff !important" in css


def test_static_files_are_always_revalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale cached app.css made a fixed button "still broken" after an upgrade:
    static files must carry Cache-Control: no-cache."""
    from fastapi.testclient import TestClient

    from orchestrator.db import session as session_module
    from orchestrator.web.app import create_app

    session_module.create_all_for_tests()
    with TestClient(create_app()) as client:
        r = client.get("/static/app.css")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert "etag" in r.headers  # revalidation is cheap: unchanged files answer 304


def test_disable_is_db_only_then_toggles_traefik_manager_on_push(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    ref = ph["extra"][ROUTE_REF_KEY]
    push(client)

    r = client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert tm.toggles == []  # the switch itself never touches Traefik Manager
    assert body["enabled"] is False and body["sync_state"] == "pending_update"
    assert any("enabled state differs" in c for c in body["sync_changes"])
    assert _row(ph["id"]).enabled is False
    assert len(_audit("proxy_host.disabled")) == 1

    result = push(client)
    assert actions(result) == [("app.example.com", "disable")]
    assert tm.toggles == [(ref, False)]
    body = client.get(f"/api/proxy-hosts/{ph['id']}").json()
    # Absent from Traefik's runtime routers on purpose: not an error.
    assert body["live_status"] == "disabled" and body["traefik"]["state"] == "disabled"
    assert body["sync_state"] == "in_sync"

    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": True})
    assert tm.toggles == [(ref, False)]
    push(client)
    assert tm.toggles[-1] == (ref, True)
    body = client.get(f"/api/proxy-hosts/{ph['id']}").json()
    assert body["traefik"]["state"] == "ok" and body["live_status"] == "matched"
    assert len(_audit("proxy_host.enabled")) == 1


def test_a_failed_toggle_is_reported_by_the_push_and_stays_pending(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    push(client)
    sig_before = _row(ph["id"]).pushed_sig
    tm.fail_toggle.add(ph["extra"][ROUTE_REF_KEY])

    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    result = push(client)
    (fail,) = failures(result)
    assert fail["action"] == "disable" and "could not disable" in fail["message"]
    assert _row(ph["id"]).enabled is False  # the DB keeps what the operator asked for
    assert _row(ph["id"]).pushed_sig == sig_before  # ...and it is still pending
    assert sync_state(client, ph["id"]) == "pending_update"


def test_editing_a_disabled_live_host_is_allowed_and_pushed_in_a_safe_order(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old "enable it first" 409 is gone: the edit lands in the DB and the
    push updates the parked route, then makes sure it is parked again."""
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    ref = ph["extra"][ROUTE_REF_KEY]
    push(client)
    client.post(f"/api/proxy-hosts/{ph['id']}/enabled", json={"enabled": False})
    push(client)
    assert ref in tm.disabled

    r = client.put(
        f"/api/proxy-hosts/{ph['id']}", json=_body(service, upstream="http://10.0.0.7:80")
    )
    assert r.status_code == 200, r.text
    assert _row(ph["id"]).upstream == "http://10.0.0.7:80"
    assert r.json()["sync_state"] == "pending_update"

    push(client)
    assert ref in tm.disabled  # still parked
    assert tm.disabled[ref]["upstream"] == "http://10.0.0.7:80"
    assert sync_state(client, ph["id"]) == "in_sync"


def test_route_disabled_on_tm_but_enabled_here_is_detached(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    ph = _live_host(client, service)
    push(client)
    ref = ph["extra"][ROUTE_REF_KEY]
    tm.disabled[ref] = tm.routes.pop(ref)  # someone flipped it in Traefik Manager
    (row,) = client.get("/api/proxy-hosts").json()
    assert row["traefik"]["state"] == "not_loaded"
    assert "Traefik Manager has this route disabled" in row["traefik"]["message"]
    assert row["sync_state"] == "detached"
    assert any("enabled state differs" in c for c in row["sync_changes"])
    # Push re-applies our state (and verifies it).
    assert actions(push(client)) == [("app.example.com", "enable")]
    assert tm.toggles[-1] == (ref, True)
    assert sync_state(client, ph["id"]) == "in_sync"
