"""Proxy-host CRUD: the DB-backed web equivalent of ``orchestrator-cli proxy``.

Auth is driven the same way as ``tests/test_auth.py``: the WebAuthn
verification step is monkeypatched, everything else (session cookie,
require_admin) runs for real. The helpers below are copied from there rather
than imported, so neither test module depends on the other's internals.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import webauthn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.registry.loader import read_yaml_file
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app


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


def test_dashboard_links_to_proxy_hosts_page(client: TestClient) -> None:
    assert 'href="/proxy-hosts"' in client.get("/").text
