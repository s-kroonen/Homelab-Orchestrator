"""The redirect companion shares the service the main route REALLY has.

Traefik Manager usually names a route's service ``<name>-service``, but a new route whose backend
another route of the file already serves can end up sharing that route's service. The redirect
router then has to refer to it by its real name: guessing ``<name>-service`` made Traefik Manager
answer ``Service '...' does not exist for HTTP routes`` (and a format run restored the old route).
"""

from __future__ import annotations

import pytest

from orchestrator.adapters.proxy.base import RouteOptions
from orchestrator.adapters.proxy.traefik_manager import (
    TraefikManagerProxyAdapter,
    router_service_of,
)
from tests.sim_tm import SimTM
from tests.test_proxy_entrypoints_adapter import _patch, _settings

UPSTREAM = "http://192.168.178.95:8123"


def _adapter(monkeypatch: pytest.MonkeyPatch, sim: SimTM) -> TraefikManagerProxyAdapter:
    _patch(monkeypatch, sim)
    return TraefikManagerProxyAdapter(_settings())


def test_router_service_of_reads_the_service_key() -> None:
    raw = "http:\n  routers:\n    r:\n      rule: Host(`a`)\n      service: shared-svc\n"
    assert router_service_of(raw, "r") == "shared-svc"
    assert router_service_of(raw.replace("shared-svc", "shared-svc@file"), "r") == "shared-svc"
    assert router_service_of(raw.replace("shared-svc", "api@internal"), "r") == ""
    assert router_service_of("not: [valid", "r") == ""
    assert router_service_of("http: {}", "r") == ""


async def test_the_redirect_follows_a_shared_service(monkeypatch: pytest.MonkeyPatch) -> None:
    sim = SimTM(files=("home.yml",), config_dir=True, strict_service_ref=True, share_services=True)
    sim.add("home.yml", "home-alt", "alt.example.lan", upstream=UPSTREAM)
    adapter = _adapter(monkeypatch, sim)
    ref = await adapter.create_route(
        hostname="home.example.lan",
        upstream=UPSTREAM,
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
        options=RouteOptions(http_redirect=True),
        config_file="home.yml",
    )
    assert ref == "home-example-lan"
    assert sim.files["home.yml"][ref]["service"] == "home-alt-service"  # shared, not its own
    redirect = [f for f in sim.calls("save") if f["serviceName"].endswith("-redirect")][-1]
    assert redirect["serviceRef"] == "home-alt-service"
    assert sim.copies_of(f"{ref}-redirect") == ["home.yml"]
    # Still editable: it points at a service Traefik Manager's form wrote (shared with the
    # route of the same backend), so a record can rewrite it.
    routes = await adapter.list_routes()
    assert routes is not None
    mine = next(r for r in routes if r.name == ref)
    assert mine.service == "home-alt-service"
    assert adapter.route_editable(mine, routes) is None


async def test_an_own_service_is_still_referred_to_by_its_usual_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM(files=("home.yml",), config_dir=True, strict_service_ref=True)
    adapter = _adapter(monkeypatch, sim)
    ref = await adapter.create_route(
        hostname="home.example.lan",
        upstream=UPSTREAM,
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
        options=RouteOptions(http_redirect=True),
        config_file="home.yml",
    )
    redirect = [f for f in sim.calls("save") if f["serviceName"].endswith("-redirect")][-1]
    assert redirect["serviceRef"] == f"{ref}-service"


async def test_a_route_that_landed_in_another_file_is_named_and_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Traefik Manager runs with fixed config paths: it cannot write home.yml and quietly uses
    # its default file instead. That is reported as it is, BEFORE the companion trips over it.
    sim = SimTM(files=("dynamic.yml", "apps.yml"), strict_service_ref=True)
    adapter = _adapter(monkeypatch, sim)
    with pytest.raises(Exception, match=r"home\.yml"):
        await adapter.create_route(
            hostname="home.example.lan",
            upstream=UPSTREAM,
            entry_points=["websecure"],
            cert_resolver="letsencrypt",
            options=RouteOptions(http_redirect=True),
            config_file="home.yml",
        )
    assert not any(sim.files[f] for f in sim.files)  # nothing left behind anywhere
