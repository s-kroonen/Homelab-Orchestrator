"""Routes are read by anyone's making and edited by their contents.

* every route is read, shown and linked: any file, other providers (Docker), HostRegexp rules;
* whether a record rewrites a route is decided by its contents (Traefik Manager's form can
  write it without loss), never by who made it or its name: a hand-written failover named
  after its hostname is read only, a route made in Traefik Manager's UI is editable;
* a new route is named after its hostname (``app-example-lan``), as Traefik Manager's form
  names one;
* an edit keeps the middlewares a person added in Traefik Manager (its ``/save`` replaces the
  whole list);
* one maintenance page per router, never on a plain-HTTP redirect router;
* no redirect router of its own when the ``web`` entry point already redirects to HTTPS.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters.proxy.base import RouteInfo, RouteOptions
from orchestrator.adapters.proxy.traefik_manager import (
    TraefikManagerProxyAdapter,
    _routes_to_route_info,
    is_redirect_router,
    route_edit_problem,
    route_name_for_hostname,
)
from orchestrator.db.models import ProxyHost, Service
from orchestrator.registry.proxy_push import (
    EXTERNAL,
    IN_SYNC,
    PENDING_CREATE,
    desired_signature,
    evaluate,
)
from orchestrator.registry.traefik_status import extract_host_patterns, rule_serves
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


def _route(name: str, service: str | None, **kw: Any) -> RouteInfo:
    app: dict[str, Any] = {
        "id": f"p.yml::{name}",
        "name": name,
        "protocol": "http",
        "rule": kw.pop("rule", "Host(`panel.example.lan`)"),
        "servers": kw.pop("servers", [UP]),
        "configFile": "p.yml",
        **({"service_name": service} if service else {}),
        **kw,
    }
    return _routes_to_route_info([app])[0]


# -- names, matching, and what is editable ---------------------------------------------------


def test_route_names_are_the_dashed_hostname() -> None:
    assert route_name_for_hostname("App.Example.lan") == "app-example-lan"
    assert route_name_for_hostname("  ") == "route"


def test_hostregexp_rules_are_matched_in_both_traefik_syntaxes() -> None:
    v3 = extract_host_patterns(r"HostRegexp(`^[a-z]+\.example\.lan$`)")
    v2 = extract_host_patterns("HostRegexp(`{sub:[a-z]+}.example.lan`)")
    for patterns in (v3, v2):
        assert rule_serves(set(), patterns, "app.example.lan")
        assert not rule_serves(set(), patterns, "app.other.lan")
        assert not rule_serves(set(), patterns, "a1.example.lan")
    assert rule_serves({"x.example.lan"}, (), "X.example.lan")
    assert extract_host_patterns("Host(`a.example.lan`)") == set()
    # A HostRegexp route is listed (it used to be dropped) and answers its hosts.
    (r,) = _routes_to_route_info(
        [{"name": "wild", "protocol": "http", "rule": "HostRegexp(`{sub:[a-z]+}.example.lan`)"}]
    )
    assert r.hostnames == set() and r.serves("app.example.lan")
    # So is a path-only rule: read, linked to no host.
    (p,) = _routes_to_route_info([{"name": "p", "protocol": "http", "rule": "PathPrefix(`/x`)"}])
    assert not p.serves("app.example.lan")


def test_a_route_is_editable_by_its_contents_not_its_name() -> None:
    form = _route("anything", "anything-service")
    assert route_edit_problem(form) is None  # made in Traefik Manager's UI: editable
    assert route_edit_problem(_route("orchestrator-x", "orchestrator-x-service")) is None
    # Shared by Traefik Manager from another route with the same backend: still the form's.
    owner = _route("other", "other-service")
    sharer = _route("panel-example-lan", "other-service")
    assert route_edit_problem(sharer, [owner, sharer]) is None
    # Named after its hostname, but a failover the form cannot write: read only.
    why = route_edit_problem(_route("panel-example-lan", "panel-failover"))
    assert why and "panel-failover" in why
    assert "plain host" in (
        route_edit_problem(
            _route("p", "p-service", rule="Host(`panel.example.lan`) && PathPrefix(`/api`)")
        )
        or ""
    )
    assert route_edit_problem(_route("p", "p-service", rule="HostRegexp(`^p\\..+$`)"))
    assert "several servers" in (
        route_edit_problem(_route("p", "p-service", servers=[UP, "http://10.0.0.10:80"])) or ""
    )
    assert "does not say" in (route_edit_problem(_route("p", None)) or "")
    docker = RouteInfo(
        name="web@docker", hostnames={"panel.example.lan"}, upstream=None, provider="docker"
    )
    assert "docker" in (route_edit_problem(docker) or "")


def test_a_redirect_router_is_editable_with_the_route_it_redirects_for() -> None:
    main = _route("panel-example-lan", "panel-example-lan-service")
    comp = _route("panel-example-lan-redirect", "panel-example-lan-service")
    assert route_edit_problem(comp, [main, comp]) is None
    failover = _route("panel-example-lan", "panel-failover")
    comp2 = _route("panel-example-lan-redirect", "panel-failover")
    assert route_edit_problem(comp2, [failover, comp2])
    assert route_edit_problem(comp, [comp])  # its route is not there
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


def test_an_adopted_failover_named_after_its_hostname_is_read_only() -> None:
    """The live gateway has exactly this: a hand-written failover whose router is named after
    the hostname. It must never be rewritten from the record (one backend)."""
    tm = TraefikManagerProxyAdapter(_settings())
    ph = _ph("panel.yml::panel-example-lan")
    hs = evaluate(tm, ph, _live("panel-example-lan", "panel-failover"))
    assert hs.state == EXTERNAL and "panel-failover" in hs.read_only
    # Any route in the form's shape is editable, whatever its name.
    for name in ("panel-example-lan", "made-in-tm"):
        ph2 = _ph(f"panel.yml::{name}")
        hs2 = evaluate(tm, ph2, _live(name, f"{name}-service"))
        assert hs2.state != EXTERNAL and hs2.read_only == ""


def test_a_vanished_route_comes_back_only_when_the_record_would_make_it_the_same() -> None:
    tm = TraefikManagerProxyAdapter(_settings())
    ours = _ph("panel-example-lan")
    ours.pushed_sig = "x:1"
    assert evaluate(tm, ours, []).state == PENDING_CREATE
    theirs = _ph("panel.yml::panel")  # linked to a route a create would not make
    theirs.pushed_sig = "x:1"
    hs = evaluate(tm, theirs, [])
    assert hs.state == EXTERNAL and "no longer exists" in hs.changes[0]
    # Traefik Manager unreadable: such a host stays read only, not a pending rewrite.
    assert evaluate(tm, theirs, None).state == EXTERNAL


def test_a_host_served_by_a_docker_router_is_linked_and_read_only() -> None:
    tm = TraefikManagerProxyAdapter(_settings())
    docker = RouteInfo(
        name="web@docker",
        route_id="web@docker",
        hostnames={"panel.example.lan"},
        upstream=None,
        provider="docker",
        service="web@docker",
        enabled=True,
    )
    ph = _ph("web@docker", enabled=False)
    hs = evaluate(tm, ph, [docker])
    # Read, linked; never switched (Traefik Manager cannot), never rewritten.
    assert hs.state == EXTERNAL and hs.route is docker and "docker" in hs.read_only
    assert hs.actions == [] and hs.host_routes == []


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
    sim.files["app.yml"]["app"]["service"] = "app"  # its own service, not the form's
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


def test_a_new_hostname_rewrites_the_rule_and_keeps_the_route(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route's name is only a label: a hostname change rewrites its rule in place."""
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
    assert r.json()["extra"]["traefik_manager_route_id"] == "app-example-com"
    result = push(client)
    assert not failures(result), result
    assert sim.files["thing.yml"]["app-example-com"]["rule"] == "Host(`new.example.com`)"


def test_an_adopted_route_made_in_traefik_manager_is_editable(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Who made it does not matter: a route in the form's shape adopted here is pushed."""
    sim = SimTM(files=("thing.yml",), config_dir=True)
    sim.add("thing.yml", "my-app", "app.example.com", upstream="http://10.0.0.9:8080")
    client = _client(monkeypatch, sim)
    created = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], adopt=True),
    )
    assert created.status_code == 201, created.text
    host = created.json()
    assert host["sync_state"] != "external" and host["read_only"] == ""
    r = client.put(
        f"/api/proxy-hosts/{host['id']}",
        json=_body(service, upstream="http://10.0.0.10:8080", entry_points=["websecure"]),
    )
    assert r.status_code == 200, r.text
    assert not failures(push(client))
    assert sim.files["thing.yml"]["my-app"]["upstream"] == "http://10.0.0.10:8080"


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
