"""Proxy-host CRUD: the DB-backed web equivalent of ``orchestrator-cli proxy``.

Auth is driven the same way as ``tests/test_auth.py``: the WebAuthn
verification step is monkeypatched, everything else (session cookie,
require_admin) runs for real. The helpers below are copied from there rather
than imported, so neither test module depends on the other's internals.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import webauthn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, Node, Probe, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.registry.loader import read_yaml_file
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app

# Captured once, at import time, before anything in this module monkeypatches
# httpx.AsyncClient — a test that patches BOTH the direct-Traefik-API client
# and the Traefik Manager client in the same test (they share one underlying
# `httpx` module object) must subclass this fixed original, not whatever
# `httpx.AsyncClient` currently is, or the second patch ends up wrapping the
# first and its transport silently wins instead.
_REAL_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _credential(raw_id: bytes) -> dict[str, Any]:
    b64 = webauthn.helpers.bytes_to_base64url(raw_id)
    return {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}}


def _fake_verify_registration(monkeypatch: pytest.MonkeyPatch, credential_id: bytes) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"pub-" + credential_id,
            sign_count=0,
            aaguid="",
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)


def _login_as_first_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap registration logs the new admin straight in."""
    _fake_verify_registration(monkeypatch, b"cred-1")
    r = client.post("/api/auth/register/begin", json={"username": "storm", "invite_token": None})
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/auth/register/complete",
        json={
            "username": "storm",
            "device_label": "test-device",
            "invite_token": None,
            "credential": _credential(b"cred-1"),
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def client(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(app) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def _audit_actions() -> list[str]:
    with Session(get_engine()) as s:
        rows = s.exec(
            select(AuditEntry).where(AuditEntry.action.startswith("proxy_host."))  # type: ignore[attr-defined]
        ).all()
        return [r.action for r in rows]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_list_get_update_delete(client: TestClient, service: Service) -> None:
    created = client.post(
        "/api/proxy-hosts",
        json={
            "hostname": "Thing.Example.com",
            "upstream": "http://10.0.0.5:8080",
            "router_provider": "traefik",
            "service_id": service.id,
            "extra": {"middlewares": ["auth"]},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["hostname"] == "thing.example.com"  # normalised
    assert body["service_slug"] == "thing"
    assert body["extra"] == {"middlewares": ["auth"]}
    ph_id = body["id"]

    listed = client.get("/api/proxy-hosts").json()
    assert [h["hostname"] for h in listed] == ["thing.example.com"]
    assert listed[0]["service_name"] == "Thing"

    assert client.get(f"/api/proxy-hosts/{ph_id}").json()["upstream"] == "http://10.0.0.5:8080"

    updated = client.put(
        f"/api/proxy-hosts/{ph_id}",
        json={
            "hostname": "thing.example.com",
            "upstream": "http://10.0.0.6:8080",
            "router_provider": "pangolin",
            "service_id": service.id,
            "extra": {},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["upstream"] == "http://10.0.0.6:8080"
    assert updated.json()["router_provider"] == "pangolin"
    assert updated.json()["service_id"] == service.id

    deleted = client.delete(f"/api/proxy-hosts/{ph_id}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/proxy-hosts/{ph_id}").status_code == 404
    assert client.get("/api/proxy-hosts").json() == []

    assert _audit_actions() == [
        "proxy_host.created",
        "proxy_host.updated",
        "proxy_host.deleted",
    ]


def test_update_audit_records_only_changed_fields(client: TestClient, service: Service) -> None:
    ph_id = client.post(
        "/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id}
    ).json()["id"]
    client.put(
        f"/api/proxy-hosts/{ph_id}",
        json={"hostname": "a.example.com", "upstream": "http://x:1", "service_id": service.id},
    )
    with Session(get_engine()) as s:
        entry = s.exec(select(AuditEntry).where(AuditEntry.action == "proxy_host.updated")).one()
    assert entry.actor == "storm"
    assert entry.target == "proxy_host:a.example.com"
    assert set(entry.details["changes"]) == {"upstream"}


def test_wake_on_hit_defaults_false_and_round_trips_through_crud(
    client: TestClient, service: Service
) -> None:
    """Defaults False — opt-in, so an upgrade never starts auto-waking a
    service nobody asked for (see docs/traffic_metrics.md)."""
    created = client.post(
        "/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id}
    )
    assert created.status_code == 201, created.text
    assert created.json()["wake_on_hit"] is False
    ph_id = created.json()["id"]

    updated = client.put(
        f"/api/proxy-hosts/{ph_id}",
        json={"hostname": "a.example.com", "service_id": service.id, "wake_on_hit": True},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["wake_on_hit"] is True

    assert client.get(f"/api/proxy-hosts/{ph_id}").json()["wake_on_hit"] is True
    assert client.get("/api/proxy-hosts").json()[0]["wake_on_hit"] is True


def test_wake_on_hit_round_trips_through_save_to_yaml(
    client: TestClient, service: Service, tmp_path: Any
) -> None:
    client.post(
        "/api/proxy-hosts",
        json={"hostname": "thing.example.com", "service_id": service.id, "wake_on_hit": True},
    )
    r = client.post("/api/registry/save")
    assert r.status_code == 200, r.text
    parsed, _ = read_yaml_file(tmp_path / "services.yaml")
    assert parsed.services[0].proxy_hosts[0].wake_on_hit is True


def test_linked_host_round_trips_through_save_to_yaml(
    client: TestClient, service: Service, tmp_path: Any
) -> None:
    """The existing Save button is what persists web edits — confirm it picks
    up a proxy host created through this API."""
    client.post(
        "/api/proxy-hosts",
        json={"hostname": "thing.example.com", "upstream": "http://t:80", "service_id": service.id},
    )
    r = client.post("/api/registry/save")
    assert r.status_code == 200, r.text
    parsed, _ = read_yaml_file(tmp_path / "services.yaml")
    hosts = parsed.services[0].proxy_hosts
    assert [(h.hostname, h.upstream) for h in hosts] == [("thing.example.com", "http://t:80")]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_service_id_is_rejected(client: TestClient) -> None:
    r = client.post("/api/proxy-hosts", json={"hostname": "x.example.com", "service_id": 9999})
    assert r.status_code == 422
    assert "9999" in r.json()["detail"]
    with Session(get_engine()) as s:
        assert s.exec(select(ProxyHost)).all() == []


def test_unknown_service_id_is_rejected_on_update(client: TestClient, service: Service) -> None:
    ph_id = client.post(
        "/api/proxy-hosts", json={"hostname": "x.example.com", "service_id": service.id}
    ).json()["id"]
    r = client.put(
        f"/api/proxy-hosts/{ph_id}", json={"hostname": "x.example.com", "service_id": 9999}
    )
    assert r.status_code == 422


def test_invalid_router_provider_is_rejected(client: TestClient, service: Service) -> None:
    r = client.post(
        "/api/proxy-hosts",
        json={
            "hostname": "x.example.com",
            "service_id": service.id,
            "router_provider": "haproxy",
        },
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "hostname",
    ["", "   ", "https://x.example.com", "x.example.com:8443", "x.example.com/path", "-bad.com"],
)
def test_malformed_hostname_is_rejected(
    client: TestClient, service: Service, hostname: str
) -> None:
    r = client.post("/api/proxy-hosts", json={"hostname": hostname, "service_id": service.id})
    assert r.status_code == 422, hostname


def test_wildcard_hostname_is_accepted(client: TestClient, service: Service) -> None:
    r = client.post(
        "/api/proxy-hosts", json={"hostname": "*.apps.example.com", "service_id": service.id}
    )
    assert r.status_code == 201, r.text


def test_duplicate_hostname_conflicts(client: TestClient, service: Service) -> None:
    assert (
        client.post(
            "/api/proxy-hosts", json={"hostname": "x.example.com", "service_id": service.id}
        ).status_code
        == 201
    )
    r = client.post(
        "/api/proxy-hosts", json={"hostname": "X.example.com", "service_id": service.id}
    )
    assert r.status_code == 409


def test_missing_record_404s(client: TestClient, service: Service) -> None:
    assert client.get("/api/proxy-hosts/123").status_code == 404
    assert (
        client.put(
            "/api/proxy-hosts/123", json={"hostname": "x.com", "service_id": service.id}
        ).status_code
        == 404
    )
    assert client.delete("/api/proxy-hosts/123").status_code == 404


# ---------------------------------------------------------------------------
# Auth + page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/proxy-hosts"),
        ("get", "/api/proxy-hosts/1"),
        ("post", "/api/proxy-hosts"),
        ("put", "/api/proxy-hosts/1"),
        ("delete", "/api/proxy-hosts/1"),
    ],
)
def test_router_401s_without_a_session(app: Any, method: str, path: str) -> None:
    with TestClient(app) as anon:
        kwargs: dict[str, Any] = {}
        if method in ("post", "put"):
            kwargs["json"] = {"hostname": "x.example.com", "service_id": 1}
        r = getattr(anon, method)(path, **kwargs)
    assert r.status_code == 401


def test_page_redirects_when_not_logged_in(app: Any) -> None:
    with TestClient(app, follow_redirects=False) as anon:
        r = anon.get("/proxy-hosts")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/proxy-hosts"


def test_page_renders_for_logged_in_admin(client: TestClient, service: Service) -> None:
    r = client.get("/proxy-hosts")
    assert r.status_code == 200
    assert "storm" in r.text
    assert "not synced with Traefik" in r.text
    assert "Thing (thing)" in r.text  # service dropdown populated
    assert "Resync from Traefik" in r.text
    assert (
        "Thing" in r.text and '"slug": "thing"' in r.text
    )  # services passed to JS for discovery rows


def test_dashboard_links_to_proxy_hosts_page(client: TestClient) -> None:
    assert 'href="/proxy-hosts"' in client.get("/").text


# ---------------------------------------------------------------------------
# Traefik drift (live_status)
# ---------------------------------------------------------------------------


def _patch_traefik_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import orchestrator.registry.traefik_status as ts_mod

    class _PatchedAsyncClient(_REAL_HTTPX_ASYNC_CLIENT):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ts_mod.httpx, "AsyncClient", _PatchedAsyncClient)


def test_live_status_is_unknown_when_traefik_not_configured(
    client: TestClient, service: Service
) -> None:
    client.post("/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id})
    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["live_status"] == "unknown"


def test_live_status_matched_when_traefik_has_the_router(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"name": "r@docker", "rule": "Host(`a.example.com`)", "status": "enabled"}]
        )

    _patch_traefik_transport(monkeypatch, handle)

    client.post("/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id})
    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["live_status"] == "matched"

    single = client.get(f"/api/proxy-hosts/{listed[0]['id']}").json()
    assert single["live_status"] == "matched"


def test_live_status_missing_when_no_matching_router(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"name": "r@docker", "rule": "Host(`other.example.com`)", "status": "enabled"}],
        )

    _patch_traefik_transport(monkeypatch, handle)

    client.post("/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id})
    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["live_status"] == "missing"


def test_live_status_unknown_when_traefik_unreachable(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_traefik_transport(monkeypatch, handle)

    client.post("/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id})
    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["live_status"] == "unknown"


# ---------------------------------------------------------------------------
# Resync (POST /api/proxy-hosts/resync)
# ---------------------------------------------------------------------------


def _traefik_responder(
    routers: list[dict[str, Any]], services: list[dict[str, Any]] | None = None
) -> Any:
    """Build a MockTransport handler serving both Traefik endpoints resync uses."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/http/routers":
            return httpx.Response(200, json=routers)
        if request.url.path == "/api/http/services":
            return httpx.Response(200, json=services if services is not None else [])
        return httpx.Response(404, json={})

    return handle


def test_resync_requires_traefik_configured(client: TestClient, service: Service) -> None:
    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 503
    assert "traefik" in r.json()["detail"].lower()


def test_resync_401s_without_a_session(app: Any) -> None:
    with TestClient(app) as anon:
        r = anon.post("/api/proxy-hosts/resync")
    assert r.status_code == 401


def test_resync_unreachable_returns_clear_error(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_traefik_transport(monkeypatch, handle)

    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 503
    assert "traefik" in r.json()["detail"].lower()


def test_resync_updates_matched_host_upstream_and_last_seen(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    created = client.post(
        "/api/proxy-hosts",
        json={
            "hostname": "a.example.com",
            "upstream": "http://stale:9999",
            "service_id": service.id,
        },
    ).json()
    assert created["last_seen_at"] is None

    _patch_traefik_transport(
        monkeypatch,
        _traefik_responder(
            routers=[
                {
                    "name": "r1@docker",
                    "rule": "Host(`a.example.com`)",
                    "service": "svc1@docker",
                    "status": "enabled",
                }
            ],
            services=[
                {
                    "name": "svc1@docker",
                    "loadBalancer": {"servers": [{"url": "http://10.0.0.5:8080"}]},
                }
            ],
        ),
    )

    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated_count"] == 1
    assert body["discovered_count"] == 0
    assert body["updated"][0]["hostname"] == "a.example.com"
    assert body["updated"][0]["upstream"] == "http://10.0.0.5:8080"
    assert body["updated"][0]["upstream_changed"] is True

    refreshed = client.get(f"/api/proxy-hosts/{created['id']}").json()
    assert refreshed["upstream"] == "http://10.0.0.5:8080"
    assert refreshed["last_seen_at"] is not None

    with Session(get_engine()) as s:
        entry = s.exec(select(AuditEntry).where(AuditEntry.action == "proxy_host.resynced")).one()
    assert entry.actor == "storm"
    assert entry.details["updated_count"] == 1
    assert entry.details["discovered_count"] == 0


def test_resync_discovered_host_is_not_persisted(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    _patch_traefik_transport(
        monkeypatch,
        _traefik_responder(
            routers=[
                {
                    "name": "r1@docker",
                    "rule": "Host(`new.example.com`)",
                    "service": "svc1@docker",
                    "status": "enabled",
                }
            ],
            services=[
                {
                    "name": "svc1@docker",
                    "loadBalancer": {"servers": [{"url": "http://10.0.0.9:80"}]},
                }
            ],
        ),
    )

    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated_count"] == 0
    assert body["discovered_count"] == 1
    assert body["discovered"][0] == {
        "hostname": "new.example.com",
        "upstream": "http://10.0.0.9:80",
        "router": "r1@docker",
    }

    with Session(get_engine()) as s:
        assert s.exec(select(ProxyHost)).all() == []


def test_resync_discover_then_create_round_trips_to_matched(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discover a hostname, create it through the existing create endpoint
    using the discovery data, and confirm the next resync treats it as
    matched rather than discovering it a second time."""
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    _patch_traefik_transport(
        monkeypatch,
        _traefik_responder(
            routers=[
                {
                    "name": "r1@docker",
                    "rule": "Host(`fresh.example.com`)",
                    "service": "svc1@docker",
                    "status": "enabled",
                }
            ],
            services=[
                {
                    "name": "svc1@docker",
                    "loadBalancer": {"servers": [{"url": "http://10.0.0.7:3000"}]},
                }
            ],
        ),
    )

    first = client.post("/api/proxy-hosts/resync").json()
    assert first["discovered_count"] == 1
    discovery = first["discovered"][0]
    assert discovery["hostname"] == "fresh.example.com"

    created = client.post(
        "/api/proxy-hosts",
        json={
            "hostname": discovery["hostname"],
            "upstream": discovery["upstream"],
            "service_id": service.id,
        },
    )
    assert created.status_code == 201, created.text

    second = client.post("/api/proxy-hosts/resync").json()
    assert second["discovered_count"] == 0
    assert second["updated_count"] == 1
    assert second["updated"][0]["hostname"] == "fresh.example.com"
    assert second["updated"][0]["upstream_changed"] is False


# ---------------------------------------------------------------------------
# Auto-managed Traefik HTTP probe (ensure_traefik_probe / sync_traefik_probe)
# ---------------------------------------------------------------------------


def _enable_proxy_base_url(monkeypatch: pytest.MonkeyPatch, url: str = "https://10.0.0.2") -> None:
    monkeypatch.setenv("PROBE_PROXY_BASE_URL", url)
    config_module.reset_settings_cache()


def _second_service() -> Service:
    """A second registered service, for re-link tests. Inserted directly —
    there's no creation API for services in this router's test surface."""
    with Session(get_engine()) as s:
        node = Node(name="eve2", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        svc = Service(
            slug="other", name="Other", node_id=node.id, guest_kind=GuestKind.VM, guest_id=200
        )
        s.add(svc)
        s.commit()
        s.refresh(svc)
        return svc


def _probes_for(service_id: int) -> list[Probe]:
    with Session(get_engine()) as s:
        return list(s.exec(select(Probe).where(Probe.service_id == service_id)).all())


def test_create_proxy_host_creates_a_non_required_http_probe(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_proxy_base_url(monkeypatch)
    r = client.post(
        "/api/proxy-hosts",
        json={"hostname": "media.example.com", "service_id": service.id},
    )
    assert r.status_code == 201, r.text

    probes = _probes_for(service.id)
    assert len(probes) == 1
    probe = probes[0]
    assert probe.name == "traefik:media.example.com"
    assert probe.kind.value == "http"
    assert probe.required is False
    assert probe.config["url"] == "https://10.0.0.2/"
    assert probe.config["host_header"] == "media.example.com"
    assert probe.config["expect_status"] == list(range(200, 500))

    assert "probe.created" in _audit_actions_all()


def test_proxy_base_url_unset_never_creates_a_probe(client: TestClient, service: Service) -> None:
    r = client.post(
        "/api/proxy-hosts", json={"hostname": "media.example.com", "service_id": service.id}
    )
    assert r.status_code == 201, r.text
    assert _probes_for(service.id) == []


def test_update_hostname_updates_host_header_without_duplicating_probe(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_proxy_base_url(monkeypatch)
    ph_id = client.post(
        "/api/proxy-hosts", json={"hostname": "old.example.com", "service_id": service.id}
    ).json()["id"]
    assert len(_probes_for(service.id)) == 1

    client.put(
        f"/api/proxy-hosts/{ph_id}",
        json={"hostname": "new.example.com", "service_id": service.id},
    )

    probes = _probes_for(service.id)
    assert len(probes) == 1
    assert probes[0].name == "traefik:new.example.com"
    assert probes[0].config["host_header"] == "new.example.com"
    assert "probe.updated" in _audit_actions_all() or "probe.deleted" in _audit_actions_all()


def test_relinking_to_a_different_service_moves_the_probe(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_proxy_base_url(monkeypatch)
    other = _second_service()
    ph_id = client.post(
        "/api/proxy-hosts", json={"hostname": "media.example.com", "service_id": service.id}
    ).json()["id"]
    assert len(_probes_for(service.id)) == 1
    assert len(_probes_for(other.id)) == 0

    client.put(
        f"/api/proxy-hosts/{ph_id}",
        json={"hostname": "media.example.com", "service_id": other.id},
    )

    assert _probes_for(service.id) == []
    moved = _probes_for(other.id)
    assert len(moved) == 1
    assert moved[0].name == "traefik:media.example.com"

    actions = _audit_actions_all()
    assert "probe.deleted" in actions
    assert "probe.created" in actions


def test_delete_proxy_host_removes_its_probe(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_proxy_base_url(monkeypatch)
    ph_id = client.post(
        "/api/proxy-hosts", json={"hostname": "media.example.com", "service_id": service.id}
    ).json()["id"]
    assert len(_probes_for(service.id)) == 1

    client.delete(f"/api/proxy-hosts/{ph_id}")

    assert _probes_for(service.id) == []
    assert "probe.deleted" in _audit_actions_all()


def test_resync_backfills_probes_for_preexisting_linked_hosts(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a ProxyHost that was linked before PROBE_PROXY_BASE_URL was
    ever configured — no probe exists yet. Enabling it and clicking Resync
    (even with Traefik unreachable... no, Resync REQUIRES Traefik; here it's
    reachable but with no matching router) must still backfill the probe."""
    ph_id = client.post(
        "/api/proxy-hosts", json={"hostname": "media.example.com", "service_id": service.id}
    ).json()["id"]
    assert _probes_for(service.id) == []  # not yet configured

    _enable_proxy_base_url(monkeypatch)
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/http/routers":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/http/services":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    _patch_traefik_transport(monkeypatch, handle)

    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text

    probes = _probes_for(service.id)
    assert len(probes) == 1
    assert probes[0].name == "traefik:media.example.com"
    assert probes[0].required is False
    assert "probe.created" in _audit_actions_all()
    _ = ph_id  # unused beyond setup


def test_auto_probe_appears_on_service_detail_as_non_required(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_proxy_base_url(monkeypatch)
    client.post(
        "/api/proxy-hosts",
        json={"hostname": "media.example.com", "service_id": service.id, "upstream": "http://u"},
    )
    client.post(
        f"/api/registry/services/{service.slug}/probes",
        json={
            "name": "manual-check",
            "kind": "tcp",
            "required": True,
            "config": {"host": "h", "port": 1},
        },
    )

    detail = client.get(f"/api/registry/services/{service.slug}").json()
    probes_by_name = {p["name"]: p for p in detail["probes"]}
    assert set(probes_by_name) == {"traefik:media.example.com", "manual-check"}
    assert probes_by_name["traefik:media.example.com"]["required"] is False
    assert probes_by_name["manual-check"]["required"] is True


def _audit_actions_all() -> list[str]:
    with Session(get_engine()) as s:
        return [r.action for r in s.exec(select(AuditEntry)).all()]


# ---------------------------------------------------------------------------
# Read path: Traefik Manager preferred over the direct Traefik API
# ---------------------------------------------------------------------------


def _patch_tm_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import orchestrator.registry.traefik_manager_client as tm_mod

    class _PatchedAsyncClient(_REAL_HTTPX_ASYNC_CLIENT):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tm_mod.httpx, "AsyncClient", _PatchedAsyncClient)


def _enable_traefik_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAEFIK_MANAGER_URL", "http://tm.test:8080")
    monkeypatch.setenv("TRAEFIK_MANAGER_API_KEY", "key")
    config_module.reset_settings_cache()


def test_live_status_matched_via_traefik_manager(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_traefik_manager(monkeypatch)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/routes"
        return httpx.Response(
            200,
            json={
                "apps": [
                    {
                        "id": "r1",
                        "name": "r1",
                        "protocol": "http",
                        "rule": "Host(`a.example.com`)",
                        "servers": ["http://10.0.0.9:80"],
                    }
                ],
                "middlewares": [],
                "services": {"http": [], "tcp": [], "udp": []},
            },
        )

    _patch_tm_transport(monkeypatch, handle)

    client.post("/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id})
    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["live_status"] == "matched"


def test_live_status_unknown_when_traefik_manager_unreachable(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_traefik_manager(monkeypatch)

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_tm_transport(monkeypatch, handle)

    client.post("/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id})
    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["live_status"] == "unknown"


def test_traefik_manager_takes_priority_over_direct_traefik_api(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BOTH are configured, Traefik Manager wins — the direct API is
    never even called."""
    _enable_traefik_manager(monkeypatch)
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    direct_api_called = False

    def direct_handle(request: httpx.Request) -> httpx.Response:
        nonlocal direct_api_called
        direct_api_called = True
        return httpx.Response(200, json=[])

    def tm_handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "apps": [
                    {
                        "id": "r1",
                        "name": "r1",
                        "protocol": "http",
                        "rule": "Host(`a.example.com`)",
                        "servers": ["http://10.0.0.9:80"],
                    }
                ],
                "middlewares": [],
                "services": {"http": [], "tcp": [], "udp": []},
            },
        )

    _patch_traefik_transport(monkeypatch, direct_handle)
    _patch_tm_transport(monkeypatch, tm_handle)

    client.post("/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id})
    listed = client.get("/api/proxy-hosts").json()
    assert listed[0]["live_status"] == "matched"
    assert direct_api_called is False


def test_resync_via_traefik_manager_updates_matched_host(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_traefik_manager(monkeypatch)

    created = client.post(
        "/api/proxy-hosts",
        json={"hostname": "a.example.com", "upstream": "http://stale:9", "service_id": service.id},
    ).json()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "apps": [
                    {
                        "id": "r1",
                        "name": "r1",
                        "protocol": "http",
                        "rule": "Host(`a.example.com`)",
                        "servers": ["http://10.0.0.5:8080"],
                    }
                ],
                "middlewares": [],
                "services": {"http": ["r1"], "tcp": [], "udp": []},
            },
        )

    _patch_tm_transport(monkeypatch, handle)

    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated_count"] == 1
    assert body["updated"][0]["upstream"] == "http://10.0.0.5:8080"

    refreshed = client.get(f"/api/proxy-hosts/{created['id']}").json()
    assert refreshed["upstream"] == "http://10.0.0.5:8080"


def test_create_proxy_host_injects_maintenance_middleware_and_audits_it(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the real router: create a ProxyHost with both
    TRAEFIK_MANAGER_URL and ORCHESTRATOR_PUBLIC_URL set, and confirm the
    middleware gets injected into a matching live route and audited."""
    _enable_traefik_manager(monkeypatch)
    monkeypatch.setenv("ORCHESTRATOR_PUBLIC_URL", "http://10.0.0.5:8000")
    config_module.reset_settings_cache()

    from orchestrator.registry.traefik_manager_sync import (
        SHARED_BACKEND_SERVICE_NAME,
        maintenance_middleware_name,
    )

    state = {
        "raw": (
            "http:\n  routers:\n    r1:\n      rule: Host(`a.example.com`)\n"
            "      service: s1\n      middlewares:\n        - existing-mw\n"
        )
    }

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/routes":
            return httpx.Response(
                200,
                json={
                    "apps": [
                        {
                            "id": "r1",
                            "name": "r1",
                            "protocol": "http",
                            "rule": "Host(`a.example.com`)",
                        }
                    ],
                    "middlewares": [],
                    "services": {"http": [SHARED_BACKEND_SERVICE_NAME], "tcp": [], "udp": []},
                },
            )
        if path == "/api/routes/r1/raw" and request.method == "GET":
            return httpx.Response(200, json={"raw": state["raw"]})
        if path == "/api/routes/r1/raw" and request.method == "POST":
            import json as _json

            state["raw"] = _json.loads(request.read())["content"]
            return httpx.Response(200, json={"ok": True})
        if path == "/save-middleware":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    _patch_tm_transport(monkeypatch, handle)

    r = client.post(
        "/api/proxy-hosts", json={"hostname": "a.example.com", "service_id": service.id}
    )
    assert r.status_code == 201, r.text

    mw_name = maintenance_middleware_name(service.slug)
    assert mw_name in state["raw"]
    assert "existing-mw" in state["raw"]  # untouched

    actions = _audit_actions_all()
    assert "middleware.created" in actions
    assert "middleware.injected" in actions


def test_resync_via_traefik_manager_surfaces_503_when_unreachable(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_traefik_manager(monkeypatch)

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_tm_transport(monkeypatch, handle)

    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 503
    assert "traefik" in r.json()["detail"].lower()
