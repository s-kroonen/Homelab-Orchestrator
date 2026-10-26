"""Route options on a live proxy host: the Host header is passed through (the
`passHostHeader: false` bug that bounced browsers to a raw LAN IP), an
``https://`` upstream keeps its scheme, wildcard certificate domains, skip-verify
and the companion HTTP -> HTTPS redirect router."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlmodel import Session

from orchestrator.adapters.proxy.base import RouteOptions
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import (
    TraefikManagerProxyAdapter,
    redirect_route_id,
)
from orchestrator.config import Settings, get_settings
from orchestrator.db.models import ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.registry.proxy_validation import validate_route_options
from orchestrator.registry.schema import ProxyHostSpec
from tests.test_proxy_entrypoints_adapter import TRAEFIK_EPS, _patch, _settings
from tests.test_proxy_entrypoints_api import FakeTM, _body, _client, _rows
from tests.test_proxy_hosts import _patch_tm_transport


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.read().decode()))


class Recorder:
    """Records every /save and /delete the adapter makes; optionally fails."""

    def __init__(
        self,
        *,
        fail_second_save: bool = False,
        missing: set[str] | None = None,
        routes: list[str] | None = None,
    ) -> None:
        # Names Traefik Manager lists in /api/routes (single config file: id == name).
        self.routes = routes or []
        self.saves: list[dict[str, str]] = []
        self.deletes: list[str] = []
        self.fail_second_save = fail_second_save
        self.missing = missing or set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/traefik/entrypoints":
            return httpx.Response(200, json=TRAEFIK_EPS)
        if path == "/api/routes":
            apps = [
                {
                    "id": n,
                    "name": n,
                    "protocol": "http",
                    "rule": f"Host(`{n}.example.com`)",
                    "servers": ["http://10.0.0.5:80"],
                }
                for n in self.routes
            ]
            return httpx.Response(
                200, json={"apps": apps, "middlewares": [], "services": {"http": []}}
            )
        if path == "/save":
            form = _form(request)
            if self.fail_second_save and form["serviceName"].endswith("-redirect"):
                return httpx.Response(400, json={"ok": False, "message": "boom"})
            if form["isEdit"] == "true" and form["serviceName"] in self.missing:
                return httpx.Response(404, json={"ok": False})
            self.saves.append(form)
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/delete/"):
            name = path.removeprefix("/delete/")
            self.deletes.append(name)
            if name in self.missing:
                return httpx.Response(404, json={"ok": False})
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})


# ---------------------------------------------------------------------------
# adapter: what is posted to Traefik Manager
# ---------------------------------------------------------------------------


async def test_default_create_posts_passhostheader_true_and_keeps_the_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder()
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.create_route(
        hostname="pbs.example.com",
        upstream="https://192.168.100.18:8007",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
    )
    (form,) = rec.saves
    # The bug: without passHostHeader=true Traefik Manager writes `false`, the
    # backend is told its own LAN address and redirects the browser to it.
    assert form["passHostHeader"] == "true"
    assert form["scheme"] == "https"  # an https upstream must not become http://
    assert (form["targetIp"], form["targetPort"]) == ("192.168.100.18", "8007")
    assert "insecureSkipVerify" not in form
    assert "tlsWildcardMain" not in form and "serviceRef" not in form


async def test_options_become_form_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.create_route(
        hostname="pbs.example.com",
        upstream="https://10.0.0.5:8007",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
        options=RouteOptions(
            pass_host_header=False,
            skip_upstream_tls_verify=True,
            tls_main="example.com",
            tls_sans=("*.example.com", "example.org"),
        ),
    )
    (form,) = rec.saves
    assert form["passHostHeader"] == "false"
    assert form["insecureSkipVerify"] == "true"
    assert form["tlsWildcardMain"] == "example.com"
    assert form["tlsWildcardSans"] == "*.example.com\nexample.org"


async def test_update_also_posts_the_options(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(routes=["orchestrator-pbs-example-com"])
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.update_route(
        "orchestrator-pbs-example-com",
        hostname="pbs.example.com",
        upstream="http://10.0.0.5:8007",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
    )
    (form,) = rec.saves
    assert form["isEdit"] == "true" and form["passHostHeader"] == "true"
    assert form["scheme"] == "http"


async def test_redirect_creates_a_companion_router_sharing_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder()
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    ref = await adapter.create_route(
        hostname="pbs.example.com",
        upstream="http://10.0.0.5:8007",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
        options=RouteOptions(http_redirect=True),
    )
    main, redirect = rec.saves
    assert main["serviceName"] == ref and main["entryPoints"] == "websecure"
    assert redirect["serviceName"] == redirect_route_id(ref)
    assert redirect["entryPoints"] == "web"  # the plain-HTTP entry point, never `traefik`
    assert redirect["certResolver"] == "__disabled__"  # no tls block on the plain router
    assert redirect["middlewares"] == "redirect-to-https"
    assert redirect["serviceRef"] == f"{ref}-service"  # shares the main route's service
    assert redirect["httpRule"] == "Host(`pbs.example.com`)"


async def test_redirect_middleware_name_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings(traefik_redirect_middleware="https-only@file"))
    await adapter.create_route(
        hostname="a.example.com",
        upstream="http://10.0.0.5:80",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
        options=RouteOptions(http_redirect=True),
    )
    assert rec.saves[1]["middlewares"] == "https-only@file"


async def test_a_failed_redirect_takes_the_main_route_back(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(fail_second_save=True)
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(Exception, match=r"boom|400"):
        await adapter.create_route(
            hostname="a.example.com",
            upstream="http://10.0.0.5:80",
            entry_points=["websecure"],
            cert_resolver="letsencrypt",
            options=RouteOptions(http_redirect=True),
        )
    # Never a half-made pair: the main route that was just written is deleted.
    assert rec.deletes == ["orchestrator-a-example-com"]


async def test_update_turning_the_redirect_off_removes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(routes=["orchestrator-a-example-com", "orchestrator-a-example-com-redirect"])
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.update_route(
        "orchestrator-a-example-com",
        hostname="a.example.com",
        upstream="http://10.0.0.5:80",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
        options=RouteOptions(http_redirect=False),
        previous_options=RouteOptions(http_redirect=True),
    )
    assert rec.deletes == ["orchestrator-a-example-com-redirect"]


async def test_update_never_blind_deletes_when_there_was_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder(routes=["orchestrator-a-example-com"])
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.update_route(
        "orchestrator-a-example-com",
        hostname="a.example.com",
        upstream="http://10.0.0.5:80",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
        options=RouteOptions(http_redirect=False),
        previous_options=RouteOptions(http_redirect=False),
    )
    assert rec.deletes == []


async def test_update_turning_the_redirect_on_creates_it_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder(routes=["orchestrator-a-example-com"])
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.update_route(
        "orchestrator-a-example-com",
        hostname="a.example.com",
        upstream="http://10.0.0.5:80",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
        options=RouteOptions(http_redirect=True),
    )
    # main edit, redirect edit refused (404) -> redirect created
    assert [(f["serviceName"], f["isEdit"]) for f in rec.saves] == [
        ("orchestrator-a-example-com", "true"),
        ("orchestrator-a-example-com-redirect", "false"),
    ]


async def test_delete_removes_the_redirect_first_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder(routes=["orchestrator-a-example-com", "orchestrator-a-example-com-redirect"])
    _patch(monkeypatch, rec)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.delete_route("orchestrator-a-example-com", with_redirect=True)
    assert rec.deletes == ["orchestrator-a-example-com-redirect", "orchestrator-a-example-com"]
    rec.deletes.clear()
    await adapter.delete_route("orchestrator-a-example-com", with_redirect=False)
    assert rec.deletes == ["orchestrator-a-example-com"]


async def test_list_domains_and_middlewares(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/settings":
            return httpx.Response(200, json={"domains": ["example.com", "example.org"]})
        if request.url.path == "/api/traefik/middlewares":
            return httpx.Response(
                200, json={"http": [{"name": "redirect-to-https@file"}, {"name": "auth@docker"}]}
            )
        return httpx.Response(404)

    _patch(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())
    assert await adapter.list_domains() == ["example.com", "example.org"]
    assert await adapter.list_middlewares() == ["redirect-to-https@file", "auth@docker"]


async def test_list_middlewares_unreachable_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, lambda r: httpx.Response(502, json={"error": "Traefik API unreachable"}))
    assert await TraefikManagerProxyAdapter(_settings()).list_middlewares() is None


async def test_dry_run_accepts_options() -> None:
    adapter = DryRunProxyAdapter()
    ref = await adapter.create_route(
        hostname="a.example.com",
        upstream="http://10.0.0.5:80",
        entry_points=["websecure"],
        options=RouteOptions(http_redirect=True),
    )
    assert ref
    assert await adapter.list_middlewares() == ["redirect-to-https@file"]


def test_maintenance_backend_keeps_its_old_host_header_behaviour() -> None:
    # It is the orchestrator's own backend; it must not start receiving the
    # visitor's Host (that would change what the maintenance page sees).
    import inspect

    from orchestrator.adapters.proxy import traefik_manager as tm

    src = inspect.getsource(tm.TraefikManagerProxyAdapter)
    assert "pass_host_header=False" in src


# ---------------------------------------------------------------------------
# RouteOptions + validation
# ---------------------------------------------------------------------------


def test_route_options_round_trip_and_defaults() -> None:
    assert RouteOptions().pass_host_header is True
    assert RouteOptions.from_dict(None) == RouteOptions()
    assert RouteOptions.from_dict({}) == RouteOptions()
    o = RouteOptions.from_dict({"tls_main": " example.com ", "tls_sans": "*.example.com, a.b"})
    assert o.tls_main == "example.com" and o.tls_sans == ("*.example.com", "a.b")
    assert RouteOptions.from_dict(o.to_dict()) == o


_EPS = ["websecure"]


def _v(options: dict[str, Any], **kw: Any) -> dict[str, str]:
    args: dict[str, Any] = {
        "upstream": "http://10.0.0.5:80",
        "entry_points": _EPS,
        "cert_resolver": "letsencrypt",
    }
    args.update(kw)
    return validate_route_options(options, **args)


def test_valid_options_have_no_errors() -> None:
    assert _v({}) == {}
    assert _v({"tls_main": "example.com", "tls_sans": ["*.example.com"]}) == {}
    assert _v({"http_redirect": True}) == {}
    assert _v({"skip_upstream_tls_verify": True}, upstream="https://10.0.0.5:8007") == {}


def test_skip_verify_needs_an_https_upstream() -> None:
    assert "skip_upstream_tls_verify" in _v({"skip_upstream_tls_verify": True})


def test_redirect_needs_an_https_entry_point() -> None:
    assert "http_redirect" in _v({"http_redirect": True}, entry_points=["web"])


def test_tls_domains_need_a_resolver_a_tls_entry_point_and_valid_names() -> None:
    assert "tls_main" in _v({"tls_main": "example.com"}, cert_resolver="")
    assert "tls_main" in _v({"tls_main": "example.com"}, entry_points=["web"])
    assert "tls_main" in _v({"tls_sans": ["*.example.com"]})  # SANs without a main
    assert "tls_main" in _v({"tls_main": "not a host!"})
    assert "tls_sans" in _v({"tls_main": "example.com", "tls_sans": ["http://bad/path"]})


def test_unknown_option_key_is_reported() -> None:
    assert "route_options" in _v({"nope": True})


def test_yaml_spec_normalises_and_rejects_unknown_keys() -> None:
    default = ProxyHostSpec(hostname="a.example.com", upstream="http://x:1", route_options={})
    explicit_default = ProxyHostSpec(
        hostname="a.example.com",
        upstream="http://x:1",
        route_options={"pass_host_header": True, "http_redirect": False},
    )
    assert default.route_options == {} == explicit_default.route_options  # hashes equal
    spec = ProxyHostSpec(
        hostname="a.example.com",
        upstream="http://x:1",
        route_options={"http_redirect": True, "tls_main": "example.com"},
    )
    assert spec.route_options["http_redirect"] is True
    with pytest.raises(ValueError, match="unknown route_options"):
        ProxyHostSpec(hostname="a.example.com", upstream="http://x:1", route_options={"x": 1})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class FakeTMWithOptions(FakeTM):
    def __init__(self, *, middlewares: list[str] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.middlewares = ["redirect-to-https@file"] if middlewares is None else middlewares

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/traefik/middlewares":
            return httpx.Response(200, json={"http": [{"name": m} for m in self.middlewares]})
        if request.url.path == "/api/settings":
            return httpx.Response(
                200, json={"cert_resolver": self.resolvers, "domains": ["example.com"]}
            )
        if request.url.path == "/save":
            # Blank fields matter here (the redirect router posts no target).
            form = dict(parse_qsl(request.read().decode(), keep_blank_values=True))
            self.saves.append(form)
            self.routes[form["serviceName"]] = {
                "rule": form["httpRule"],
                "eps": [e for e in form["entryPoints"].split(",") if e],
                "upstream": f"{form['scheme']}://{form['targetIp']}:{form['targetPort']}",
                "provider": "file",
                "certResolver": form.get("certResolver"),
            }
            return httpx.Response(200, json={"ok": True})
        return super().__call__(request)


def test_create_stores_the_options_and_sends_them(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.push_helpers import push

    fake = FakeTMWithOptions()
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(
            service,
            upstream="https://10.0.0.9:8006",
            entry_points=["websecure"],
            cert_resolver="letsencrypt",
            route_options={
                "skip_upstream_tls_verify": True,
                "http_redirect": True,
                "tls_main": "example.com",
                "tls_sans": ["*.example.com"],
            },
        ),
    )
    assert r.status_code == 201, r.text
    assert fake.saves == []  # stored now, sent on the push
    stored = _rows()[0].route_options
    assert stored["http_redirect"] is True and stored["tls_main"] == "example.com"
    assert r.json()["route_options"]["pass_host_header"] is True

    push(client)
    main = fake.saves[0]
    assert main["scheme"] == "https" and main["passHostHeader"] == "true"
    assert main["insecureSkipVerify"] == "true"
    assert main["tlsWildcardMain"] == "example.com"
    assert fake.saves[1]["serviceName"].endswith("-redirect")


def test_a_new_host_is_made_the_standard_way(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host header passed and, because Traefik has the middleware, the HTTP -> HTTPS redirect
    router - the same as the new-host form and what "Format routes" rewrites old routes into."""
    from tests.push_helpers import push

    fake = FakeTMWithOptions()
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"]))
    assert r.status_code == 201, r.text
    assert _rows()[0].route_options["http_redirect"] is True
    assert r.json()["route_options"]["pass_host_header"] is True
    push(client)
    assert fake.saves[0]["passHostHeader"] == "true"
    assert len(fake.saves) == 2  # the main router and its redirect router


def test_without_the_redirect_middleware_a_new_host_has_no_redirect_and_stores_nothing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.push_helpers import push

    fake = FakeTMWithOptions(middlewares=[])
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"]))
    assert r.status_code == 201, r.text
    assert _rows()[0].route_options == {}  # all-defaults stays empty
    push(client)
    assert len(fake.saves) == 1


def test_redirect_with_a_missing_middleware_is_refused_before_anything_is_written(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTMWithOptions(middlewares=[])
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], route_options={"http_redirect": True}),
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert "redirect-to-https" in errors["http_redirect"]
    assert fake.saves == [] and _rows() == []


def test_invalid_options_are_a_field_level_422(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTMWithOptions()
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(
            service,
            entry_points=["web"],
            route_options={"http_redirect": True, "skip_upstream_tls_verify": True},
        ),
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert "http_redirect" in errors and "skip_upstream_tls_verify" in errors
    assert fake.saves == []


def test_editing_a_legacy_host_fixes_the_host_header(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported incident: a route created before this change has no stored
    options. Re-saving it must now write passHostHeader=true."""
    from tests.push_helpers import push

    fake = FakeTMWithOptions()
    client = _client(monkeypatch, fake)
    created = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"]))
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    push(client)
    with Session(get_engine()) as s:
        ph = s.get(ProxyHost, pid)
        assert ph is not None
        ph.route_options = {}  # as every pre-existing row is
        s.add(ph)
        s.commit()
    fake.saves.clear()
    body = _body(service, upstream="https://10.0.0.9:8006", entry_points=["websecure"])
    r = client.put(f"/api/proxy-hosts/{pid}", json=body)
    assert r.status_code == 200, r.text
    assert fake.saves == []
    push(client)
    assert fake.saves[0]["isEdit"] == "true"
    assert fake.saves[0]["passHostHeader"] == "true"
    assert fake.saves[0]["scheme"] == "https"


def test_edit_keeps_stored_options_when_the_body_omits_them(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.push_helpers import push

    fake = FakeTMWithOptions()
    client = _client(monkeypatch, fake)
    created = client.post(
        "/api/proxy-hosts",
        json=_body(
            service,
            entry_points=["websecure"],
            route_options={"pass_host_header": False},
        ),
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    push(client)
    assert fake.saves[0]["passHostHeader"] == "false"
    fake.saves.clear()
    r = client.put(
        f"/api/proxy-hosts/{pid}",
        json=_body(service, entry_points=["websecure"], upstream="http://10.0.0.9:9000"),
    )
    assert r.status_code == 200, r.text
    push(client)
    assert fake.saves[0]["passHostHeader"] == "false"  # kept, not silently reset


def test_turning_the_redirect_off_in_an_edit_removes_the_router(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.push_helpers import push

    fake = FakeTMWithOptions()
    client = _client(monkeypatch, fake)
    created = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], route_options={"http_redirect": True}),
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    push(client)
    assert "orchestrator-app-example-com-redirect" in fake.routes
    fake.deletes.clear()
    r = client.put(
        f"/api/proxy-hosts/{pid}",
        json=_body(service, entry_points=["websecure"], route_options={"http_redirect": False}),
    )
    assert r.status_code == 200, r.text
    assert fake.deletes == []  # the edit itself touches nothing
    push(client)
    assert fake.deletes == ["orchestrator-app-example-com-redirect"]
    assert _rows()[0].route_options == {}


def test_deleting_a_host_with_a_redirect_removes_both_routers(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.push_helpers import push

    fake = FakeTMWithOptions()
    client = _client(monkeypatch, fake)
    created = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], route_options={"http_redirect": True}),
    )
    pid = created.json()["id"]
    push(client)
    fake.deletes.clear()
    assert client.delete(f"/api/proxy-hosts/{pid}").status_code == 200
    assert fake.deletes == []
    push(client)
    assert fake.deletes == ["orchestrator-app-example-com-redirect", "orchestrator-app-example-com"]


def test_catalog_endpoint_offers_domains_and_redirect_availability(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, FakeTMWithOptions())
    body = client.get("/api/proxy/entrypoints").json()
    assert body["domains"] == ["example.com"]
    assert body["redirect_middleware"] == "redirect-to-https"
    assert body["redirect_available"] is True
    _patch_tm_transport(monkeypatch, FakeTMWithOptions(middlewares=[]))
    refreshed = client.get("/api/proxy/entrypoints?refresh=true").json()
    assert refreshed["redirect_available"] is False


# ---------------------------------------------------------------------------
# migration 0012
# ---------------------------------------------------------------------------


def test_migration_adds_route_options_with_a_safe_default() -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "0011_proxy_host_entrypoints")
    engine = sa.create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO proxy_host (hostname, upstream, router_provider, extra, "
                "entry_points, cert_resolver) "
                "VALUES ('old.example.com', 'http://x:1', 'traefik', '{}', '[]', '')"
            )
        )
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        value = conn.execute(
            sa.text("SELECT route_options FROM proxy_host WHERE hostname='old.example.com'")
        ).scalar_one()
    assert value in ("{}", {})
    command.downgrade(cfg, "0011_proxy_host_entrypoints")
    with engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("proxy_host")}
    assert "route_options" not in cols


def test_settings_default_redirect_middleware() -> None:
    assert Settings().traefik_redirect_middleware == "redirect-to-https"


def test_hsts_is_refused_when_traefik_lacks_the_middleware(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTMWithOptions(middlewares=["redirect-to-https"])
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], route_options={"hsts": True}),
    )
    assert r.status_code == 422
    assert "hsts-headers" in r.json()["detail"]["errors"]["hsts"]
    assert fake.saves == [] and _rows() == []


def test_hsts_is_sent_when_traefik_has_the_middleware(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.push_helpers import push

    fake = FakeTMWithOptions(middlewares=["redirect-to-https", "hsts-headers"])
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], route_options={"hsts": True}),
    )
    assert r.status_code == 201, r.text
    assert _rows()[0].route_options["hsts"] is True
    push(client)
    assert fake.saves[0]["middlewares"] == "hsts-headers"


def test_hsts_needs_an_https_entry_point(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTMWithOptions(middlewares=["redirect-to-https", "hsts-headers"])
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["web"], route_options={"hsts": True}),
    )
    assert r.status_code == 422 and "hsts" in r.json()["detail"]["errors"]
