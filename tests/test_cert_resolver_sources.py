"""Certificate resolvers: the union of every place one can be seen.

Before, only Traefik Manager's own ``cert_resolver`` setting counted, so a
resolver declared only in Traefik's static configuration (``certificatesResolvers``)
was invisible and refused by the validator. Now the list is the union of

* ``tm_settings``     - ``GET /api/settings`` ``cert_resolver``;
* ``static_config``   - ``GET /api/static/config`` ``parsed.certificatesResolvers``
  (only when Traefik Manager has its static config path set - else 404);
* ``runtime_router``  - ``tls.certResolver`` of the routers Traefik serves;
* ``default_setting`` - ``TRAEFIK_DEFAULT_CERT_RESOLVER``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from orchestrator import config as config_module
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter
from orchestrator.cli import cli
from orchestrator.db.models import Service
from tests.sim_tm import SimTM
from tests.test_proxy_entrypoints_adapter import _patch, _settings
from tests.test_proxy_entrypoints_api import FakeTM, _body, _client


def sources(
    monkeypatch: pytest.MonkeyPatch, sim: Any, **settings: Any
) -> dict[str, list[str]] | None:
    _patch(monkeypatch, sim)
    return asyncio.run(
        TraefikManagerProxyAdapter(_settings(**settings)).list_cert_resolver_sources()
    )


# ---------------------------------------------------------------------------
# Each source on its own
# ---------------------------------------------------------------------------


def test_source_a_traefik_manager_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    got = sources(monkeypatch, SimTM(resolvers="cloudflare, letsencrypt,,none"))
    assert got == {"cloudflare": ["tm_settings"], "letsencrypt": ["tm_settings"]}


def test_source_b_static_config_certificates_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    got = sources(monkeypatch, SimTM(static_resolvers=["letsencrypt-dns", "http01"]))
    assert got == {"letsencrypt-dns": ["static_config"], "http01": ["static_config"]}


def test_source_c_resolvers_routers_really_use(monkeypatch: pytest.MonkeyPatch) -> None:
    got = sources(monkeypatch, SimTM(runtime_only_resolvers=("acme-edge",)))
    assert got == {"acme-edge": ["runtime_router"]}


def test_source_d_the_configured_default_is_added_when_something_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got = sources(monkeypatch, SimTM(resolvers="le"), traefik_default_cert_resolver="mine")
    assert got == {"le": ["tm_settings"], "mine": ["default_setting"]}


# ---------------------------------------------------------------------------
# Union, de-duplication, stable order, tags per name
# ---------------------------------------------------------------------------


def test_union_is_deduplicated_tagged_and_in_stable_order(monkeypatch: pytest.MonkeyPatch) -> None:
    sim = SimTM(
        resolvers="letsencrypt, cf",
        static_resolvers=["cf", "dns"],
        runtime_only_resolvers=("dns", "wild"),
    )
    got = sources(monkeypatch, sim, traefik_default_cert_resolver="wild")
    assert got == {
        "letsencrypt": ["tm_settings"],
        "cf": ["tm_settings", "static_config"],
        "dns": ["static_config", "runtime_router"],
        "wild": ["runtime_router", "default_setting"],
    }
    assert list(got) == ["letsencrypt", "cf", "dns", "wild"]  # order of first sight

    # ...and it is the same on every read.
    again = sources(monkeypatch, sim, traefik_default_cert_resolver="wild")
    assert again == got


def test_a_resolver_that_a_router_uses_through_a_created_route_is_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM()
    sim.add("dynamic.yml", "r", "r.example.com")
    sim.files["dynamic.yml"]["r"]["resolver"] = "from-a-route"
    assert sources(monkeypatch, sim) == {"from-a-route": ["runtime_router"]}


# ---------------------------------------------------------------------------
# A source that cannot be read adds nothing - it is never an error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 500, 401])
def test_static_config_not_available_is_graceful(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    sim = SimTM(resolvers="le", static_resolvers=["hidden"], static_status=status)
    assert sources(monkeypatch, sim) == {"le": ["tm_settings"]}


def test_static_config_with_an_unexpected_shape_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/static/config":
            return httpx.Response(200, json={"raw": "x", "parsed": ["not", "a", "mapping"]})
        if request.url.path == "/api/settings":
            return httpx.Response(200, json={"cert_resolver": "le"})
        return httpx.Response(200, json={"ok": True})

    assert sources(monkeypatch, handler) == {"le": ["tm_settings"]}

    def no_resolver_key(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/static/config":
            return httpx.Response(200, json={"raw": "", "parsed": {"entryPoints": {}}})
        return httpx.Response(401, json={"error": "no"})

    # Static config readable but declares no resolver: that IS a readable source.
    assert sources(monkeypatch, no_resolver_key) == {}


def test_a_network_failure_on_one_source_does_not_hide_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/static/config":
            raise httpx.ConnectError("boom", request=request)
        if request.url.path == "/api/settings":
            return httpx.Response(200, json={"cert_resolver": "le"})
        return httpx.Response(502, json={"error": "Traefik API unreachable"})

    assert sources(monkeypatch, handler) == {"le": ["tm_settings"]}


def test_only_the_static_config_readable_still_gives_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM(static_resolvers=["only-here"], settings_status=401)
    assert sources(monkeypatch, sim) == {"only-here": ["static_config"]}


def test_none_only_when_no_source_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "no"})

    assert sources(monkeypatch, down) is None
    # The configured default alone proves nothing about what exists: free text.
    assert sources(monkeypatch, down, traefik_default_cert_resolver="mine") is None
    # Not configured at all.
    assert sources(monkeypatch, down, traefik_manager_url="") is None
    # Readable-but-empty is not "unknown".
    assert sources(monkeypatch, SimTM()) == {}


def test_list_cert_resolvers_is_the_names_of_the_union(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, SimTM(resolvers="le", static_resolvers=["dns"]))
    adapter = TraefikManagerProxyAdapter(_settings())
    assert asyncio.run(adapter.list_cert_resolvers()) == ["le", "dns"]


def test_runtime_routers_expose_their_cert_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, SimTM(runtime_only_resolvers=("acme-edge",)))
    routers = asyncio.run(TraefikManagerProxyAdapter(_settings()).list_runtime_routers())
    assert routers is not None
    assert [r.cert_resolver for r in routers] == ["acme-edge"]


# ---------------------------------------------------------------------------
# Catalog endpoint, validation, refresh
# ---------------------------------------------------------------------------


def test_catalog_returns_names_and_sources(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(resolvers="letsencrypt", static_resolvers=["letsencrypt-dns"])
    client = _client(monkeypatch, sim)
    body = client.get("/api/proxy/entrypoints").json()
    assert body["cert_resolvers"] == ["letsencrypt", "letsencrypt-dns"]
    assert body["cert_resolvers_known"] is True
    assert body["cert_resolver_sources"] == {
        "letsencrypt": ["tm_settings"],
        "letsencrypt-dns": ["static_config"],
    }
    assert body["cert_resolver_source_labels"]["static_config"] == "static config"


def test_free_text_fallback_when_nothing_is_readable(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Blind(FakeTM):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path in ("/api/settings", "/api/static/config"):
                return httpx.Response(401, json={"error": "no"})
            return super().__call__(request)

    client = _client(monkeypatch, Blind(runtime_available=False))
    body = client.get("/api/proxy/entrypoints").json()
    assert body["cert_resolvers_known"] is False and body["cert_resolvers"] == []
    assert body["cert_resolver_sources"] == {}
    # Nothing to validate against: any well-formed name is accepted.
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], cert_resolver="anything-goes"),
    )
    assert r.status_code == 201, r.text


def test_validation_accepts_a_resolver_that_only_the_static_config_declares(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(resolvers="letsencrypt", static_resolvers=["letsencrypt-dns"])
    client = _client(monkeypatch, sim)
    ok = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], cert_resolver="letsencrypt-dns"),
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["cert_resolver"] == "letsencrypt-dns"

    bad = client.post(
        "/api/proxy-hosts",
        json=_body(
            service, hostname="b.example.com", entry_points=["websecure"], cert_resolver="nope"
        ),
    )
    assert bad.status_code == 422, bad.text
    message = bad.json()["detail"]["errors"]["cert_resolver"]
    assert "nope" in message and "letsencrypt, letsencrypt-dns" in message


def test_a_resolver_added_a_moment_ago_is_accepted_without_waiting_for_the_cache(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(resolvers="letsencrypt")
    client = _client(monkeypatch, sim)
    client.get("/api/proxy/entrypoints")  # fills the 30 s cache without it
    sim.static_resolvers = ["brand-new"]  # the operator adds it to Traefik's static config
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], cert_resolver="brand-new"),
    )
    assert r.status_code == 201, r.text


def test_push_status_and_resync_refresh_the_catalog(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(resolvers="letsencrypt")
    client = _client(monkeypatch, sim)
    first = client.get("/api/proxy/entrypoints").json()["cert_resolvers"]
    assert first == ["letsencrypt"]

    sim.static_resolvers = ["added-1"]
    # still cached...
    assert client.get("/api/proxy/entrypoints").json()["cert_resolvers"] == ["letsencrypt"]
    # ...until the push status is read
    assert client.get("/api/push/status").status_code == 200
    assert client.get("/api/proxy/entrypoints").json()["cert_resolvers"] == [
        "letsencrypt",
        "added-1",
    ]

    sim.static_resolvers = ["added-1", "added-2"]
    assert client.post("/api/proxy-hosts/resync").status_code == 200
    assert client.get("/api/proxy/entrypoints").json()["cert_resolvers"][-1] == "added-2"

    sim.static_resolvers = ["added-1", "added-2", "added-3"]
    assert (
        client.get("/api/proxy/entrypoints", params={"refresh": "true"}).json()["cert_resolvers"][
            -1
        ]
        == "added-3"
    )


def test_the_form_shows_the_source_of_each_resolver(client_ui: TestClient) -> None:
    html = client_ui.get("/proxy-hosts/new").text
    assert "cert_resolver_sources" in html and "Seen in:" in html
    assert "not known to Traefik" in html  # the free-text fallback keeps its marker
    assert 'id="ph-resolver-text"' in html  # ...and the free-text field itself


@pytest.fixture
def client_ui(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _client(monkeypatch, SimTM(resolvers="letsencrypt"))


# ---------------------------------------------------------------------------
# CLI + dry-run
# ---------------------------------------------------------------------------


def test_dry_run_adapter_tags_its_resolvers() -> None:
    got = asyncio.run(DryRunProxyAdapter().list_cert_resolver_sources())
    assert got == {"letsencrypt": ["tm_settings"], "letsencrypt-dns": ["static_config"]}


def test_cli_proxy_resolvers_and_entrypoints_list_names_with_sources() -> None:
    runner = CliRunner()
    r = runner.invoke(cli, ["proxy", "resolvers"])  # DRY_RUN adapter
    assert r.exit_code == 0, r.output
    assert "Certificate resolvers:" in r.output
    assert "letsencrypt " in r.output and "Traefik Manager settings" in r.output
    assert "letsencrypt-dns" in r.output and "static config" in r.output

    r = runner.invoke(cli, ["proxy", "entrypoints"])
    assert r.exit_code == 0, r.output
    assert "letsencrypt-dns" in r.output and "static config" in r.output


def test_cli_proxy_resolvers_exits_1_when_nothing_is_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PROXY_ADAPTER", "traefik_manager")
    monkeypatch.setenv("TRAEFIK_MANAGER_URL", "http://tm.test:8080")
    monkeypatch.setenv("TRAEFIK_MANAGER_API_KEY", "k")
    config_module.reset_settings_cache()
    _patch(monkeypatch, lambda request: httpx.Response(401, json={"error": "no"}))
    r = CliRunner().invoke(cli, ["proxy", "resolvers"])
    assert r.exit_code == 1
    assert "(unknown - no source could be read)" in r.output
