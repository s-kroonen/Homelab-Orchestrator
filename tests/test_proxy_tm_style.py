"""Hosts the orchestrator makes look like hosts Traefik Manager's own route form makes.

* a new route is named after its hostname (``app-example-lan``, service
  ``app-example-lan-service``), no ``orchestrator-`` prefix; older prefixed routes stay ours;
* a route is ours by name AND shape: a hand-written failover that happens to be named after
  its hostname is never rewritten;
* an edit keeps the middlewares a person added in Traefik Manager (its ``/save`` replaces the
  whole list);
* one maintenance page per router, never on a plain-HTTP redirect router;
* no redirect router of its own when the ``web`` entry point already redirects to HTTPS.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters.proxy.base import RouteOptions
from orchestrator.adapters.proxy.traefik_manager import (
    TraefikManagerProxyAdapter,
    _routes_to_route_info,
    is_redirect_router,
    route_name_for_hostname,
)
from orchestrator.db.models import ProxyHost, Service
from orchestrator.registry.proxy_push import (
    EXTERNAL,
    IN_SYNC,
    desired_signature,
    evaluate,
    route_is_ours,
)
from tests.push_helpers import actions, failures, push
from tests.sim_tm import SimTM
from tests.test_proxy_entrypoints_adapter import _patch, _settings
from tests.test_proxy_entrypoints_api import _body, _client

UP = "http://10.0.0.9:8080"


def _adapter(monkeypatch: pytest.MonkeyPatch, sim: SimTM) -> TraefikManagerProxyAdapter:
    _patch(monkeypatch, sim)
    return TraefikManagerProxyAdapter(_settings())


def _saves(sim: SimTM) -> list[dict[str, str]]:
    return [dict(form) for form in sim.calls("save")]


# -- names and ownership --------------------------------------------------------------------


def test_route_names_are_the_dashed_hostname() -> None:
    assert route_name_for_hostname("App.Example.lan") == "app-example-lan"
    assert route_name_for_hostname("  ") == "route"


def test_a_route_is_ours_by_its_name_and_shape() -> None:
    tm = TraefikManagerProxyAdapter(_settings())
    host = "panel.example.lan"
    # Not created yet (nothing to look at): the name decides.
    assert tm.owns_route_ref("panel-example-lan", host)
    assert tm.owns_route_ref("panel.yml::panel-example-lan", host)
    # The shape Traefik Manager's form writes: its own service, or a shared one.
    assert tm.owns_route_ref("panel-example-lan", host, service="panel-example-lan-service@file")
    assert tm.owns_route_ref("panel-example-lan", host, service="other-example-lan-service")
    # Named like ours but hand-written: a failover the form cannot make.
    assert not tm.owns_route_ref("panel-example-lan", host, service="panel-failover@file")
    # Another host's name, or no hostname to compare with.
    assert not tm.owns_route_ref("panel-example-lan", "other.example.lan")
    assert not tm.owns_route_ref("panel-example-lan")
    assert not tm.owns_route_ref("panel", host)
    # Made by an older version: the prefix still makes it ours, whatever it serves now.
    assert tm.owns_route_ref("orchestrator-panel-example-lan")
    assert tm.owns_route_ref("f.yml::orchestrator-old-example-lan", host, service="x")


def test_a_redirect_companion_is_judged_by_the_service_it_shares() -> None:
    tm = TraefikManagerProxyAdapter(_settings())

    def route(name: str, service: str) -> Any:
        return _routes_to_route_info(
            [
                {
                    "id": f"p.yml::{name}",
                    "name": name,
                    "protocol": "http",
                    "rule": "Host(`panel.example.lan`)",
                    "servers": [UP],
                    "service_name": service,
                    "configFile": "p.yml",
                }
            ]
        )[0]

    assert route_is_ours(tm, route("panel-example-lan-redirect", "panel-example-lan-service"))
    assert not route_is_ours(tm, route("panel-example-lan-redirect", "panel-failover"))
    assert not route_is_ours(tm, route("panel-example-lan", "panel-failover"))
    assert is_redirect_router("x-redirect")
    assert is_redirect_router("x", ["redirect-to-https@file"], "redirect-to-https")
    assert not is_redirect_router("x", ["hsts-headers"], "redirect-to-https")


def _ph(ref: str, **kw: Any) -> ProxyHost:
    return ProxyHost(
        hostname="panel.example.lan",
        upstream="http://10.0.0.14:80",
        entry_points=["websecure"],
        extra={"traefik_manager_route_id": ref},
        **kw,
    )


def _live(name: str, service: str) -> Any:
    return _routes_to_route_info(
        [
            {
                "id": f"panel.yml::{name}",
                "name": name,
                "protocol": "http",
                "rule": "Host(`panel.example.lan`)",
                "servers": ["http://10.0.0.14:80"],
                "entryPoints": ["websecure"],
                "service_name": service,
                "middlewares": [],
                "configFile": "panel.yml",
            }
        ]
    )


def test_an_adopted_failover_named_after_its_hostname_stays_external() -> None:
    """The live gateway has exactly this: a hand-written failover whose router is named after
    the hostname. It must never be rewritten from the record (one backend)."""
    tm = TraefikManagerProxyAdapter(_settings())
    ph = _ph("panel.yml::panel-example-lan")
    assert evaluate(tm, ph, _live("panel-example-lan", "panel-failover")).state == EXTERNAL
    # The same name pointing at the service Traefik Manager's form writes is ours.
    ours = evaluate(tm, ph, _live("panel-example-lan", "panel-example-lan-service"))
    assert ours.state != EXTERNAL


def test_a_missing_redirect_router_is_no_difference_when_the_entry_point_redirects() -> None:
    tm = TraefikManagerProxyAdapter(_settings())
    ph = _ph("panel-example-lan", route_options={"http_redirect": True, "pass_host_header": True})
    ph.pushed_sig = desired_signature(ph)
    live = _live("panel-example-lan", "panel-example-lan-service")
    assert "redirect router is missing" in " ".join(evaluate(tm, ph, live).changes)
    tm._ep_redirect = True  # what list_entrypoints saw
    assert evaluate(tm, ph, live).state == IN_SYNC


# -- the adapter ----------------------------------------------------------------------------


async def test_a_new_route_is_named_like_traefik_managers_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM(files=("app.yml",), config_dir=True)
    tm = _adapter(monkeypatch, sim)
    ref = await tm.create_route(
        hostname="app.example.lan",
        upstream=UP,
        entry_points=["websecure"],
        options=RouteOptions(http_redirect=True),
        config_file="app.yml",
    )
    assert ref == "app-example-lan"
    assert sim.copies_of("app-example-lan") == ["app.yml"]
    assert sim.copies_of("app-example-lan-redirect") == ["app.yml"]  # web does not redirect


async def test_no_redirect_router_when_the_entry_point_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM(files=("app.yml",), config_dir=True, entrypoint_redirect=True)
    tm = _adapter(monkeypatch, sim)
    eps = await tm.list_entrypoints()
    assert eps is not None and eps[0].redirect_to == "websecure"
    assert tm.entrypoint_redirects_http()
    await tm.create_route(
        hostname="app.example.lan",
        upstream=UP,
        entry_points=["websecure"],
        options=RouteOptions(http_redirect=True),
        config_file="app.yml",
    )
    assert [f["serviceName"] for f in _saves(sim)] == ["app-example-lan"]
    # An edit adds none either...
    await tm.update_route(
        "app-example-lan",
        hostname="app.example.lan",
        upstream=UP,
        entry_points=["websecure"],
        options=RouteOptions(http_redirect=True),
    )
    assert sim.copies_of("app-example-lan-redirect") == []
    # ...and one that is there already is left alone until the option is turned off.
    sim.add("app.yml", "app-example-lan-redirect", "app.example.lan", eps=("web",))
    await tm.update_route(
        "app-example-lan",
        hostname="app.example.lan",
        upstream=UP,
        entry_points=["websecure"],
        options=RouteOptions(http_redirect=True),
    )
    assert sim.copies_of("app-example-lan-redirect") == ["app.yml"]


async def test_an_edit_keeps_the_middlewares_added_in_traefik_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM(files=("app.yml",), config_dir=True, report_middlewares=True)
    sim.add(
        "app.yml",
        "app-example-lan",
        "app.example.lan",
        middlewares=("crowdsec@file", "maintenance-thing@file", "hsts-headers"),
    )
    tm = _adapter(monkeypatch, sim)

    async def edit(**opts: Any) -> str:
        await tm.update_route(
            "app.yml::app-example-lan",
            hostname="app.example.lan",
            upstream="http://10.0.0.10:8080",
            entry_points=["websecure"],
            options=RouteOptions(**opts),
        )
        return _saves(sim)[-1]["middlewares"]

    # The maintenance page is left to the push (it puts the linked service's back).
    assert await edit(hsts=True) == "crowdsec@file,hsts-headers"
    assert await edit(hsts=False) == "crowdsec@file"
    assert await edit(hsts=True) == "crowdsec@file,hsts-headers"


async def test_one_maintenance_page_per_router_and_none_on_the_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM(files=("app.yml",), config_dir=True, report_middlewares=True)
    sim.add(
        "app.yml",
        "app-example-lan",
        "app.example.lan",
        middlewares=("crowdsec@file", "maintenance-old@file", "hsts-headers"),
    )
    sim.add(
        "app.yml",
        "app-example-lan-redirect",
        "app.example.lan",
        eps=("web",),
        middlewares=("redirect-to-https@file",),
    )
    sim.add("app.yml", "app", "app.example.lan", middlewares=())  # hand-written: never edited
    tm = _adapter(monkeypatch, sim)
    events = await tm.ensure_maintenance_fallback(
        hostname="app.example.lan", service_slug="thing", raise_errors=True
    )
    injected = [e.details["route_id"] for e in events if e.action == "middleware.injected"]
    assert injected == ["app.yml::app-example-lan"]
    main = sim.files["app.yml"]["app-example-lan"]
    assert sim.middlewares_of("app-example-lan", main) == [
        "crowdsec@file",
        "maintenance-thing",
        "hsts-headers",
    ]
    assert sim.middlewares_of(
        "app-example-lan-redirect", sim.files["app.yml"]["app-example-lan-redirect"]
    ) == ["redirect-to-https@file"]
    assert sim.middlewares_of("app", sim.files["app.yml"]["app"]) == []
    # Again: nothing to write.
    again = await tm.ensure_maintenance_fallback(
        hostname="app.example.lan", service_slug="thing", raise_errors=True
    )
    assert not [e for e in again if e.action == "middleware.injected"]


# -- through the API ------------------------------------------------------------------------


def test_a_new_host_gets_a_route_named_after_it(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("thing.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    created = client.post(
        "/api/proxy-hosts", json=_body(service, entry_points=["websecure"])
    ).json()
    assert created["extra"]["traefik_manager_route_id"] == "app-example-com"
    assert actions(push(client)) == [("app.example.com", "create")]
    assert sim.copies_of("app-example-com") == ["thing.yml"]


def test_a_new_hostname_replaces_the_route_named_after_the_old_one(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("thing.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    created = client.post(
        "/api/proxy-hosts", json=_body(service, entry_points=["websecure"])
    ).json()
    assert not failures(push(client))
    r = client.put(
        f"/api/proxy-hosts/{created['id']}",
        json=_body(service, hostname="new.example.com", entry_points=["websecure"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["extra"]["traefik_manager_route_id"] == "new-example-com"
    result = push(client)
    assert not failures(result), result
    assert sim.copies_of("new-example-com") == ["thing.yml"]
    assert sim.copies_of("app-example-com") == []  # the old route went with the old name


def test_the_form_defaults_the_redirect_off_when_the_entry_point_redirects(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("thing.yml",), config_dir=True, entrypoint_redirect=True)
    client: TestClient = _client(monkeypatch, sim)
    cat = client.get("/api/proxy/entrypoints?refresh=true").json()
    assert cat["entrypoint_redirect"] is True
    assert cat["entrypoints"][0] == {"name": "web", "address": ":80", "redirect_to": "websecure"}


def test_a_pending_deletion_never_removes_a_route_only_named_like_ours(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlmodel import Session

    from orchestrator.db.session import get_engine
    from orchestrator.registry.proxy_push import add_pending_delete

    sim = SimTM(files=("panel.yml",), config_dir=True, report_middlewares=True)
    sim.add("panel.yml", "panel-example-com", "panel.example.com")
    sim.files["panel.yml"]["panel-example-com"]["service"] = "panel-failover"
    client = _client(monkeypatch, sim)
    with Session(get_engine()) as s:
        add_pending_delete(
            s, hostname="panel.example.com", route_ref="panel-example-com", with_redirect=True
        )
        s.commit()
    result = push(client)
    assert not failures(result), result
    assert "left panel-example-com alone" in result["results"][0]["message"]
    assert sim.copies_of("panel-example-com") == ["panel.yml"]
    assert not sim.calls("delete")
