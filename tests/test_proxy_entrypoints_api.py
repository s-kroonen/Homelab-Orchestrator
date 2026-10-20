"""Proxy hosts end to end against a small simulated Traefik Manager + Traefik:
explicit entry points, validation before anything is written, conflict check,
post-save verification and rollback.

``FakeTM`` reproduces the behaviour of the incident: ``/save`` happily stores
any entry point, and Traefik then flags the router as broken (``entryPoint
"https" doesn't exist``) in its runtime router list.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.db import session as session_module
from orchestrator.db.models import ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin, _patch_tm_transport, _tm_client

ROUTE_REF_KEY = "traefik_manager_route_id"
TM_ENV = {"TRAEFIK_MANAGER_URL": "http://tm.test:8080", "TRAEFIK_MANAGER_API_KEY": "key"}

DEFAULT_EPS = [
    {"name": "web", "address": ":80"},
    {"name": "websecure", "address": ":443"},
    {"name": "traefik", "address": ":8080"},
]


class FakeTM:
    def __init__(
        self,
        *,
        entrypoints: list[dict[str, str]] | None = None,
        resolvers: str = "letsencrypt",
        runtime_available: bool = True,
        reject_all: bool = False,
    ) -> None:
        self.entrypoints = DEFAULT_EPS if entrypoints is None else entrypoints
        self.entrypoints_down = False
        self.resolvers = resolvers
        self.runtime_available = runtime_available
        self.reject_all = reject_all  # Traefik refuses every router (post-save)
        self.routes: dict[str, dict[str, Any]] = {}
        self.saves: list[dict[str, str]] = []
        self.deletes: list[str] = []
        self.entrypoint_calls = 0

    def seed(self, name: str, hostname: str, eps: list[str], provider: str = "file") -> None:
        self.routes[name] = {
            "rule": f"Host(`{hostname}`)",
            "eps": eps,
            "upstream": "http://1.2.3.4:80",
            "provider": provider,
        }

    def _runtime(self) -> list[dict[str, Any]]:
        valid = {e["name"] for e in self.entrypoints}
        out = []
        for name, route in self.routes.items():
            bad = [e for e in route["eps"] if e not in valid]
            errors: list[str] = []
            if bad:
                errors = [f'entryPoint "{e}" doesn\'t exist' for e in bad] + [
                    "no valid entryPoint for this router"
                ]
            elif self.reject_all:
                errors = ["simulated Traefik refusal"]
            out.append(
                {
                    "name": f"{name}@{route['provider']}",
                    "rule": route["rule"],
                    "service": f"{name}-service@{route['provider']}",
                    "entryPoints": route["eps"],
                    "status": "disabled" if errors else "enabled",
                    **({"error": errors} if errors else {}),
                }
            )
        return out

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/traefik/entrypoints":
            self.entrypoint_calls += 1
            if self.entrypoints_down:
                return httpx.Response(502, json={"error": "Traefik API unreachable"})
            return httpx.Response(200, json=self.entrypoints)
        if path == "/api/settings":
            return httpx.Response(200, json={"cert_resolver": self.resolvers})
        if path == "/api/routes":
            apps = [
                {
                    "id": n,
                    "name": n,
                    "protocol": "http",
                    "rule": r["rule"],
                    "servers": [r["upstream"]],
                    "entryPoints": r["eps"],
                }
                for n, r in self.routes.items()
                if r["provider"] == "file"  # TM only manages the file provider
            ]
            return httpx.Response(
                200, json={"apps": apps, "middlewares": [], "services": {"http": []}}
            )
        if path == "/api/traefik/routers":
            if not self.runtime_available:
                return httpx.Response(502, json={"error": "Traefik API unreachable"})
            return httpx.Response(
                200, json={"http": self._runtime(), "tcp": [], "udp": [], "reachable": True}
            )
        if path == "/save":
            form = dict(parse_qsl(request.read().decode()))
            self.saves.append(form)
            self.routes[form["serviceName"]] = {
                "rule": form["httpRule"],
                "eps": [e for e in form["entryPoints"].split(",") if e],
                "upstream": f"http://{form['targetIp']}:{form['targetPort']}",
                "provider": "file",
                "certResolver": form.get("certResolver"),
            }
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/delete/"):
            name = path.removeprefix("/delete/")
            self.deletes.append(name)
            if name not in self.routes:
                return httpx.Response(404, json={"ok": False})
            del self.routes[name]
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})


def _client(monkeypatch: pytest.MonkeyPatch, fake: FakeTM, **env: str) -> TestClient:
    client = _tm_client(monkeypatch, **{**TM_ENV, **env})
    _patch_tm_transport(monkeypatch, fake)
    return client


def _body(service: Service, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "hostname": "app.example.com",
        "upstream": "http://10.0.0.9:8080",
        "service_id": service.id,
    }
    body.update(over)
    return body


def _rows() -> list[ProxyHost]:
    with Session(get_engine()) as s:
        return list(s.exec(select(ProxyHost)).all())


# ---------------------------------------------------------------------------
# GET /api/proxy/entrypoints
# ---------------------------------------------------------------------------


def test_entrypoints_endpoint_shape(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTM(resolvers="letsencrypt, cloudflare")
    client = _client(monkeypatch, fake)
    r = client.get("/api/proxy/entrypoints")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "entrypoints": [
            {"name": "web", "address": ":80"},
            {"name": "websecure", "address": ":443"},
            {"name": "traefik", "address": ":8080"},
        ],
        "cert_resolvers": ["letsencrypt", "cloudflare"],
        "cert_resolvers_known": True,
        "default_entrypoints": ["websecure"],
        "default_cert_resolver": "",
        "domains": [],
        "redirect_middleware": "redirect-to-https",
        "redirect_available": None,
        "source": "traefik_manager",
    }


def test_entrypoints_endpoint_is_cached_and_refresh_busts_it(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    client.get("/api/proxy/entrypoints")
    client.get("/api/proxy/entrypoints")
    assert fake.entrypoint_calls == 1
    fake.entrypoints = [{"name": "edge", "address": ":443"}]
    assert client.get("/api/proxy/entrypoints").json()["entrypoints"][0]["name"] == "web"
    fresh = client.get("/api/proxy/entrypoints?refresh=true").json()
    assert fresh["entrypoints"] == [{"name": "edge", "address": ":443"}]
    assert fresh["default_entrypoints"] == ["edge"]  # first non-traefik, no websecure
    assert fake.entrypoint_calls == 2


def test_entrypoints_endpoint_unavailable(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.entrypoints_down = True
    client = _client(monkeypatch, fake)
    body = client.get("/api/proxy/entrypoints").json()
    assert body["source"] == "unavailable"
    assert body["entrypoints"] == [] and body["default_entrypoints"] == []


@pytest.fixture
def dry_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Logged-in client on the default dry-run proxy adapter."""
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_entrypoints_endpoint_requires_admin() -> None:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as anon:
        assert anon.get("/api/proxy/entrypoints").status_code in (401, 403)


def test_default_setting_wins_when_valid_and_is_ignored_when_not(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _client(
        monkeypatch,
        FakeTM(),
        TRAEFIK_DEFAULT_ENTRYPOINTS="web",
        TRAEFIK_DEFAULT_CERT_RESOLVER="letsencrypt",
    )
    body = c.get("/api/proxy/entrypoints").json()
    assert body["default_entrypoints"] == ["web"] and body["default_cert_resolver"] == "letsencrypt"


def test_default_setting_that_does_not_exist_falls_back_to_websecure(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _client(
        monkeypatch,
        FakeTM(),
        TRAEFIK_DEFAULT_ENTRYPOINTS="https",
        TRAEFIK_DEFAULT_CERT_RESOLVER="nope",
    )
    body = c.get("/api/proxy/entrypoints").json()
    assert body["default_entrypoints"] == ["websecure"] and body["default_cert_resolver"] == ""


# ---------------------------------------------------------------------------
# create: explicit fields, validation, conflict, verification
# ---------------------------------------------------------------------------


def test_create_sends_the_chosen_entry_points_and_resolver_and_stores_them(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], cert_resolver="letsencrypt"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert fake.saves[0]["entryPoints"] == "websecure"
    assert fake.saves[0]["certResolver"] == "letsencrypt"
    assert body["entry_points"] == ["websecure"] and body["cert_resolver"] == "letsencrypt"
    assert body["entry_points_stored"] is True
    assert body["traefik"]["state"] == "ok" and body["warnings"] == []
    assert _rows()[0].entry_points == ["websecure"]


def test_create_without_entry_points_resolves_the_default_rule_never_blank(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service))
    assert r.status_code == 201, r.text
    assert fake.saves[0]["entryPoints"] == "websecure"  # not TM's `https`
    assert fake.saves[0]["certResolver"] == "__none__"  # TLS, no resolver chosen


def test_create_on_plain_web_sends_no_tls(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["web"]))
    assert r.status_code == 201, r.text
    assert fake.saves[0]["entryPoints"] == "web"
    assert fake.saves[0]["certResolver"] == "__disabled__"


def test_create_without_a_choice_and_without_a_live_list_is_refused(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.entrypoints_down = True
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service))
    assert r.status_code == 422
    assert "entry_points" in r.json()["detail"]["errors"]
    assert fake.saves == [] and _rows() == []


def test_create_unreadable_list_trusts_the_typed_entry_points(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.entrypoints_down = True
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points="websecure"))
    assert r.status_code == 201, r.text
    assert fake.saves[0]["entryPoints"] == "websecure"


def test_unknown_entry_point_is_refused_before_anything_is_written(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["https"]))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["errors"] == {
        "entry_points": 'Entry point "https" does not exist on this Traefik. '
        "Available: web, websecure, traefik"
    }
    assert fake.saves == [] and _rows() == []


def test_internal_traefik_entry_point_is_refused(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, FakeTM())
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["traefik"]))
    assert r.status_code == 422
    assert "internal" in r.json()["detail"]["errors"]["entry_points"]


def test_unknown_cert_resolver_is_refused(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, FakeTM())
    r = client.post(
        "/api/proxy-hosts", json=_body(service, entry_points=["websecure"], cert_resolver="nope")
    )
    assert r.status_code == 422
    assert "nope" in r.json()["detail"]["errors"]["cert_resolver"]


def test_422_reports_every_bad_field_at_once(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, hostname="https://x/y", upstream="10.0.0.9", entry_points=["https"]),
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert set(detail["errors"]) == {"hostname", "upstream", "entry_points"}
    assert detail["message"]  # a single readable line for the toast
    assert fake.saves == []


def test_duplicate_hostname_is_409_with_the_same_error_shape(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, FakeTM())
    assert client.post("/api/proxy-hosts", json=_body(service)).status_code == 201
    r = client.post("/api/proxy-hosts", json=_body(service, hostname="APP.example.com"))
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]["errors"]["hostname"]


def test_create_refuses_to_overwrite_a_router_that_is_not_ours(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.seed("hand-made", "app.example.com", ["websecure"])
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"]))
    assert r.status_code == 422
    msg = r.json()["detail"]["errors"]["hostname"]
    assert "hand-made" in msg and "Resync" in msg
    assert fake.saves == [] and _rows() == []


def test_create_refuses_when_the_route_name_is_taken(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.seed("orchestrator-app-example-com", "other.example.com", ["web"])
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["web"]))
    assert r.status_code == 422
    assert "orchestrator-app-example-com" in r.json()["detail"]["errors"]["hostname"]


def test_create_refuses_a_hostname_served_by_another_provider(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.seed("dockerthing", "app.example.com", ["websecure"], provider="docker")
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"]))
    assert r.status_code == 422
    assert "dockerthing@docker" in r.json()["detail"]["errors"]["hostname"]


def test_create_rolls_back_when_traefik_rejects_the_router(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM(reject_all=True)
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"]))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["traefik_error"] == "simulated Traefik refusal"
    assert "simulated Traefik refusal" in detail["message"]
    assert detail["rolled_back"] is True
    assert fake.deletes == ["orchestrator-app-example-com"]  # the TM route was removed again
    assert fake.routes == {}
    assert _rows() == []  # no DB row committed


def test_create_reports_the_exact_traefik_text_for_a_stale_entry_point(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident: our list said `https` was fine (stale cache), Traefik says no."""
    fake = FakeTM(entrypoints=[*DEFAULT_EPS, {"name": "https", "address": ":8443"}])
    client = _client(monkeypatch, fake)
    client.get("/api/proxy/entrypoints")  # warm the cache with `https` present
    fake.entrypoints = DEFAULT_EPS  # ...then Traefik drops it
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["https"]))
    assert r.status_code == 422, r.text
    err = r.json()["detail"]["traefik_error"]
    assert err == 'entryPoint "https" doesn\'t exist · no valid entryPoint for this router'
    assert "entry_points" in r.json()["detail"]["errors"]
    assert _rows() == [] and fake.routes == {}


def test_create_warns_when_it_cannot_verify(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM(runtime_available=False)
    client = _client(monkeypatch, fake)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"]))
    assert r.status_code == 201, r.text
    warnings = r.json()["warnings"]
    assert len(warnings) == 1 and warnings[0].startswith("Saved, but could not be verified")
    assert len(_rows()) == 1


def test_create_warns_when_traefik_has_not_loaded_the_router(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    base_call = fake.__call__

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/traefik/routers":
            return httpx.Response(200, json={"http": [], "reachable": True})
        return base_call(request)

    client = _tm_client(monkeypatch, **TM_ENV)
    _patch_tm_transport(monkeypatch, handle)
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"]))
    assert r.status_code == 201, r.text
    assert "not loaded the router yet" in r.json()["warnings"][0]


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


def _create(client: TestClient, service: Service, **over: Any) -> dict[str, Any]:
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"], **over))
    assert r.status_code == 201, r.text
    return r.json()


def test_edit_resends_the_stored_entry_points_when_the_request_omits_them(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    created = _create(client, service, cert_resolver="letsencrypt")
    fake.saves.clear()
    r = client.put(
        f"/api/proxy-hosts/{created['id']}", json=_body(service, upstream="http://10.0.0.9:9999")
    )
    assert r.status_code == 200, r.text
    assert fake.saves[0]["entryPoints"] == "websecure"  # NOT reset to `https`
    assert fake.saves[0]["certResolver"] == "letsencrypt"
    assert fake.saves[0]["isEdit"] == "true"


def test_edit_can_change_entry_points(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    created = _create(client, service)
    r = client.put(
        f"/api/proxy-hosts/{created['id']}",
        json=_body(service, entry_points=["web", "websecure"], cert_resolver=""),
    )
    assert r.status_code == 200, r.text
    assert r.json()["entry_points"] == ["web", "websecure"]
    assert fake.saves[-1]["entryPoints"] == "web,websecure"


def test_edit_failure_keeps_the_new_config_and_returns_traefiks_message(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    created = _create(client, service)
    fake.reject_all = True
    r = client.put(
        f"/api/proxy-hosts/{created['id']}", json=_body(service, upstream="http://10.0.0.9:9999")
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["traefik_error"] == "simulated Traefik refusal"
    assert detail["rolled_back"] is False
    # The new configuration is on Traefik Manager and in our DB: kept.
    assert _rows()[0].upstream == "http://10.0.0.9:9999"
    assert fake.routes["orchestrator-app-example-com"]["upstream"] == "http://10.0.0.9:9999"
    assert fake.deletes == []
    # ...and the list shows it red with Traefik's own text.
    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["traefik"]["state"] == "error"
    assert listed[0]["traefik"]["message"] == "simulated Traefik refusal"


def test_list_shows_the_broken_router_error_text_for_a_preexisting_route(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's incident: a route saved with the defaulted `https`."""
    fake = FakeTM()
    fake.seed("orchestrator-app-example-com", "app.example.com", ["https"])
    client = _client(monkeypatch, fake)
    with Session(get_engine()) as s:
        s.add(
            ProxyHost(
                hostname="app.example.com",
                upstream="http://1.2.3.4:80",
                service_id=service.id,
                extra={ROUTE_REF_KEY: "orchestrator-app-example-com"},
            )
        )
        s.commit()
    row = client.get("/api/proxy-hosts").json()[0]
    assert row["entry_points"] == [] and row["entry_points_stored"] is False
    assert row["traefik"]["state"] == "error"
    assert row["traefik"]["message"] == (
        'entryPoint "https" doesn\'t exist · no valid entryPoint for this router'
    )
    # The edit form pre-selects from what the route really has.
    assert row["traefik"]["entry_points"] == ["https"]


def test_edit_of_a_legacy_host_with_a_broken_route_makes_you_pick_a_real_entry_point(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.seed("orchestrator-app-example-com", "app.example.com", ["https"])
    client = _client(monkeypatch, fake)
    with Session(get_engine()) as s:
        ph = ProxyHost(
            hostname="app.example.com",
            upstream="http://1.2.3.4:80",
            service_id=service.id,
            extra={ROUTE_REF_KEY: "orchestrator-app-example-com"},
        )
        s.add(ph)
        s.commit()
        s.refresh(ph)
        ph_id = ph.id
    # Omitting entry points falls back to what the route has - `https` - which is refused.
    r = client.put(f"/api/proxy-hosts/{ph_id}", json=_body(service))
    assert r.status_code == 422
    assert 'Entry point "https" does not exist' in r.json()["detail"]["errors"]["entry_points"]
    # Picking the right one fixes the route.
    r = client.put(f"/api/proxy-hosts/{ph_id}", json=_body(service, entry_points=["websecure"]))
    assert r.status_code == 200, r.text
    assert fake.routes["orchestrator-app-example-com"]["eps"] == ["websecure"]
    assert r.json()["traefik"]["state"] == "ok"


def test_edit_hostname_change_to_one_traefik_already_serves_is_refused(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    created = _create(client, service)
    fake.seed("someone-else", "taken.example.com", ["web"])
    r = client.put(
        f"/api/proxy-hosts/{created['id']}", json=_body(service, hostname="taken.example.com")
    )
    assert r.status_code == 422
    assert "someone-else" in r.json()["detail"]["errors"]["hostname"]


def test_unchanged_edit_is_not_blocked_by_its_own_router(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    created = _create(client, service)
    r = client.put(f"/api/proxy-hosts/{created['id']}", json=_body(service, entry_points=["web"]))
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# adopt (Resync's "Discovered")
# ---------------------------------------------------------------------------


def test_adopt_records_an_existing_route_without_writing_to_traefik_manager(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.seed("hand-made", "app.example.com", ["websecure"])
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts", json=_body(service, upstream="http://1.2.3.4:80", adopt=True)
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert fake.saves == [] and fake.deletes == []
    assert body["extra"][ROUTE_REF_KEY] == "hand-made"
    assert body["entry_points"] == ["websecure"]
    assert body["traefik"]["state"] == "ok"


def test_resync_discovers_entry_points_and_reports_per_host_router_status(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    created = _create(client, service)
    fake.seed("hand-made", "other.example.com", ["web"])
    calls_before = fake.entrypoint_calls
    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"][0]["traefik"]["state"] == "ok"
    assert body["discovered"][0]["entry_points"] == ["web"]
    assert fake.entrypoint_calls > calls_before  # Resync busts the catalog cache
    assert created["hostname"] == body["updated"][0]["hostname"]


# ---------------------------------------------------------------------------
# dry run + pages
# ---------------------------------------------------------------------------


def test_dry_run_create_edit_flow_end_to_end(dry_client: TestClient, service: Service) -> None:
    r = dry_client.post("/api/proxy-hosts", json=_body(service))
    assert r.status_code == 201, r.text
    assert r.json()["entry_points"] == ["websecure"]
    assert r.json()["traefik"]["state"] == "ok"
    bad = dry_client.post(
        "/api/proxy-hosts", json=_body(service, hostname="b.example.com", entry_points=["https"])
    )
    assert bad.status_code == 422
    assert "Available: web, websecure, traefik" in bad.json()["detail"]["errors"]["entry_points"]


def test_new_form_renders_the_entry_point_controls(
    dry_client: TestClient, service: Service
) -> None:
    html = dry_client.get("/proxy-hosts/new").text
    for marker in (
        'id="ep-list"',
        'id="ep-text"',
        'id="ph-resolver"',
        "Refresh from Traefik Manager",
        'data-error-for="entry_points"',
        'data-error-for="hostname"',
        "/api/proxy/entrypoints",
    ):
        assert marker in html, marker


def test_edit_form_carries_the_stored_selection(dry_client: TestClient, service: Service) -> None:
    created = dry_client.post(
        "/api/proxy-hosts", json=_body(service, entry_points=["web"], cert_resolver="")
    ).json()
    html = dry_client.get(f"/proxy-hosts/{created['id']}/edit").text
    assert '"entry_points": ["web"]' in html and '"stored": true' in html
    legacy = dry_client.post(
        "/api/proxy-hosts", json=_body(service, hostname="l.example.com", upstream="")
    )
    html = dry_client.get(f"/proxy-hosts/{legacy.json()['id']}/edit").text
    assert '"stored": false' in html and "Not stored yet" in html


def test_list_page_has_entry_point_and_router_status_columns(dry_client: TestClient) -> None:
    html = dry_client.get("/proxy-hosts").text
    assert ">Entry points</th>" in html
    assert "appendRouterStatus" in html and "router-error" in html
    assert "table-scroll" in html
