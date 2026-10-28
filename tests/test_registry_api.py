"""Registry CRUD over the dashboard API: services, nodes, probes.

Everything here is DB-only — the YAML round trip is the existing Save/Reset
endpoints' job, exercised once at the end to prove the edits survive it.

Auth follows ``tests/test_auth.py``: only WebAuthn's verification step is
faked; the session cookie and ``require_admin`` run for real.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import webauthn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, BackupRecord, Node, Probe, Service
from orchestrator.db.session import get_engine
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _fake_verify_registration(monkeypatch: pytest.MonkeyPatch, credential_id: bytes) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"pub-" + credential_id,
            sign_count=0,
            aaguid="",
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)


def _register(client: TestClient, monkeypatch: pytest.MonkeyPatch, username: str) -> None:
    """Bootstrap the first admin; registering also logs the client in."""
    credential_id = b"cred-" + username.encode()
    _fake_verify_registration(monkeypatch, credential_id)
    r = client.post("/api/auth/register/begin", json={"username": username})
    assert r.status_code == 200, r.text
    b64 = webauthn.helpers.bytes_to_base64url(credential_id)
    r = client.post(
        "/api/auth/register/complete",
        json={
            "username": username,
            "device_label": "test-device",
            "credential": {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}},
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def client(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(app) as c:
        _register(c, monkeypatch, "storm")
        yield c


def _make_node(client: TestClient, name: str = "pve1", **extra: Any) -> dict[str, Any]:
    r = client.post("/api/registry/nodes", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _make_service(
    client: TestClient, node_id: int, slug: str = "media", **extra: Any
) -> dict[str, Any]:
    body = {"slug": slug, "name": slug.title(), "node_id": node_id, "guest_kind": "vm"}
    r = client.post("/api/registry/services", json={**body, **extra})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _audit_actions() -> list[str]:
    with Session(get_engine()) as s:
        return [e.action for e in s.exec(select(AuditEntry).order_by(AuditEntry.id)).all()]


HTTP_CONFIG = {"url": "http://10.0.0.5/health", "expect_status": [200]}
SYSTEMD_CONFIG = {"unit": "nginx.service", "transport": {"type": "host_agent", "host": "media"}}


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/registry/services/media"),
        ("POST", "/api/registry/services"),
        ("PUT", "/api/registry/services/media"),
        ("DELETE", "/api/registry/services/media"),
        ("GET", "/api/registry/nodes"),
        ("POST", "/api/registry/nodes"),
        ("PUT", "/api/registry/nodes/pve1"),
        ("DELETE", "/api/registry/nodes/pve1"),
        ("GET", "/api/registry/services/media/probes"),
        ("POST", "/api/registry/services/media/probes"),
        ("PUT", "/api/registry/probes/1"),
        ("DELETE", "/api/registry/probes/1"),
    ],
)
def test_registry_crud_401s_without_a_session(app: Any, method: str, path: str) -> None:
    with TestClient(app) as anon:
        r = anon.request(method, path, json={})
    assert r.status_code == 401


def test_service_detail_page_redirects_when_not_logged_in(app: Any) -> None:
    with TestClient(app, follow_redirects=False) as anon:
        r = anon.get("/services/media")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def test_node_create_update_delete(client: TestClient) -> None:
    node = _make_node(client, "pve1", always_on=True, power_mgr_target="plug/pve1")
    assert node["always_on"] is True

    listing = client.get("/api/registry/nodes").json()
    assert [(n["name"], n["service_count"]) for n in listing] == [("pve1", 0)]

    r = client.put("/api/registry/nodes/pve1", json={"name": "pve-main", "notes": "rack top"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "pve-main"
    assert r.json()["notes"] == "rack top"
    assert r.json()["always_on"] is True  # untouched by a partial update

    assert client.delete("/api/registry/nodes/pve-main").status_code == 200
    assert client.get("/api/registry/nodes").json() == []
    assert _audit_actions()[-3:] == ["node.created", "node.updated", "node.deleted"]


def test_node_wake_on_hit_enabled_defaults_true_and_is_editable(client: TestClient) -> None:
    """Default True — it is a safety rail on top of the per-proxy-host
    opt-in (which defaults False), not an independent opt-in, so leaving it
    untouched must never block a wake-on-hit someone turned on elsewhere."""
    node = _make_node(client, "pve1")
    assert node["wake_on_hit_enabled"] is True

    r = client.put("/api/registry/nodes/pve1", json={"wake_on_hit_enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["wake_on_hit_enabled"] is False
    # Untouched by a partial update that doesn't mention it.
    r = client.put("/api/registry/nodes/pve1", json={"notes": "x"})
    assert r.json()["wake_on_hit_enabled"] is False


def test_node_duplicate_name_is_rejected(client: TestClient) -> None:
    _make_node(client, "pve1")
    _make_node(client, "pve2")
    assert client.post("/api/registry/nodes", json={"name": "pve1"}).status_code == 409
    assert client.put("/api/registry/nodes/pve2", json={"name": "pve1"}).status_code == 409


def test_node_in_use_cannot_be_deleted(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])
    r = client.delete("/api/registry/nodes/pve1")
    assert r.status_code == 409
    assert "media" in r.json()["detail"]


def test_unknown_node_404s(client: TestClient) -> None:
    assert client.put("/api/registry/nodes/nope", json={"notes": "x"}).status_code == 404
    assert client.delete("/api/registry/nodes/nope").status_code == 404


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def test_service_detail_flags_wake_unsupported_node(client: TestClient) -> None:
    """Surfaced up front on the service detail page — see
    docs/architecture.md's wake-unsupported section — not only discovered
    when a visitor actually hits a down backend."""
    bare_node = _make_node(client, "bare")  # no power_mgr_target, not always_on
    _make_service(client, bare_node["id"], slug="unsupported-svc")
    assert client.get("/api/registry/services/unsupported-svc").json()["wake_unsupported"] is True

    on_node = _make_node(client, "on", always_on=True)
    _make_service(client, on_node["id"], slug="fine-svc")
    assert client.get("/api/registry/services/fine-svc").json()["wake_unsupported"] is False


def test_service_create_get_update_delete(client: TestClient) -> None:
    node = _make_node(client)
    created = _make_service(client, node["id"], guest_id=101)
    assert created["slug"] == "media"
    assert created["node"]["name"] == "pve1"
    assert created["probes"] == []

    detail = client.get("/api/registry/services/media").json()
    assert detail["guest_id"] == 101
    assert detail["enabled"] is True

    _make_service(client, node["id"], slug="gateway")
    r = client.put(
        "/api/registry/services/media",
        json={
            "name": "Media server",
            "enabled": False,
            "backup_excluded": True,
            "backup_excluded_reason": "hosts the PBS datastore",
            "depends_on": ["gateway"],
            "guest_kind": "ct",
            "guest_id": 202,
        },
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["name"] == "Media server"
    assert updated["enabled"] is False
    assert updated["backup_excluded"] is True
    assert updated["depends_on"] == ["gateway"]
    assert updated["guest_kind"] == "ct"
    assert updated["guest_id"] == 202
    assert updated["description"] == ""  # untouched

    r = client.delete("/api/registry/services/media")
    assert r.status_code == 200, r.text
    assert client.get("/api/registry/services/media").status_code == 404
    actions = _audit_actions()
    assert "service.created" in actions
    assert "service.updated" in actions
    assert actions[-1] == "service.deleted"


def test_service_wake_on_hit_enabled_defaults_true_and_is_editable(client: TestClient) -> None:
    node = _make_node(client)
    created = _make_service(client, node["id"])
    assert created["wake_on_hit_enabled"] is True

    r = client.put("/api/registry/services/media", json={"wake_on_hit_enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["wake_on_hit_enabled"] is False


def test_service_group_create_update_and_default(client: TestClient) -> None:
    """``group`` defaults to "" (ungrouped), is settable at create, and is a
    plain editable field like description — not validated against anything."""
    node = _make_node(client)
    created = _make_service(client, node["id"])
    assert created["group"] == ""

    grouped = _make_service(client, node["id"], slug="media2", group="media")
    assert grouped["group"] == "media"

    r = client.put("/api/registry/services/media2", json={"group": "entertainment"})
    assert r.status_code == 200, r.text
    assert r.json()["group"] == "entertainment"

    # None clears it back to "", same convention as description.
    r = client.put("/api/registry/services/media2", json={"group": None})
    assert r.status_code == 200, r.text
    assert r.json()["group"] == ""


def test_service_audit_records_the_admin(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])
    with Session(get_engine()) as s:
        entry = s.exec(select(AuditEntry).where(AuditEntry.action == "service.created")).one()
    assert entry.actor == "storm"
    assert entry.target == "service:media"


def test_service_duplicate_slug_is_rejected(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])
    r = client.post(
        "/api/registry/services", json={"slug": "media", "name": "Again", "node_id": node["id"]}
    )
    assert r.status_code == 409


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"node_id": 999}, "node_id 999"),
        ({"slug": "Bad Slug!"}, "invalid slug"),
        ({"name": "   "}, "name"),
        ({"guest_kind": "potato"}, "guest_kind"),
        ({"backup_policy_id": 42}, "backup_policy_id"),
        ({"depends_on": ["ghost"]}, "ghost"),
        ({"depends_on": ["media"]}, "itself"),
        ({"backup_excluded": True}, "reason"),
    ],
)
def test_service_create_validation(
    client: TestClient, override: dict[str, Any], fragment: str
) -> None:
    node = _make_node(client)
    body = {"slug": "media", "name": "Media", "node_id": node["id"], **override}
    r = client.post("/api/registry/services", json=body)
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]


def test_service_update_rejects_unknown_node_and_cycles(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], slug="a")
    _make_service(client, node["id"], slug="b", depends_on=["a"])

    r = client.put("/api/registry/services/a", json={"node_id": 999})
    assert r.status_code == 400
    r = client.put("/api/registry/services/a", json={"depends_on": ["b"]})
    assert r.status_code == 400
    assert "cycle" in r.json()["detail"]
    r = client.put("/api/registry/services/a", json={})
    assert r.status_code == 400
    assert client.put("/api/registry/services/zzz", json={"name": "x"}).status_code == 404


def test_service_depended_on_cannot_be_deleted(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], slug="gateway")
    _make_service(client, node["id"], slug="media", depends_on=["gateway"])
    r = client.delete("/api/registry/services/gateway")
    assert r.status_code == 409
    assert "media" in r.json()["detail"]


def test_service_delete_cascades_probes_but_keeps_backup_history(client: TestClient) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"])
    r = client.post(
        "/api/registry/services/media/probes",
        json={"name": "http", "kind": "http", "config": HTTP_CONFIG},
    )
    assert r.status_code == 201, r.text
    with Session(get_engine()) as s:
        s.add(BackupRecord(service_id=svc["id"], service_slug="media", pbs_snapshot_id="vm/1/x"))
        s.commit()

    r = client.delete("/api/registry/services/media")
    assert r.status_code == 200
    assert r.json()["probes_removed"] == 1

    with Session(get_engine()) as s:
        assert s.exec(select(Probe)).all() == []
        record = s.exec(select(BackupRecord)).one()
        assert record.service_id is None
        assert record.service_slug == "media"


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def test_probe_create_update_delete(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])

    r = client.post(
        "/api/registry/services/media/probes",
        json={"name": "web", "kind": "http", "config": HTTP_CONFIG},
    )
    assert r.status_code == 201, r.text
    probe = r.json()
    assert probe["timeout_s"] == 10  # PROBE_SPECS default for http
    assert probe["order"] == 0
    assert probe["required"] is True

    r = client.post(
        "/api/registry/services/media/probes",
        json={"name": "unit", "kind": "systemd", "required": False, "config": SYSTEMD_CONFIG},
    )
    assert r.status_code == 201, r.text
    assert r.json()["order"] == 1  # appended

    r = client.put(
        f"/api/registry/probes/{probe['id']}",
        json={"kind": "tcp", "config": {"host": "10.0.0.5", "port": 443}, "timeout_s": 3},
    )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "tcp"
    assert r.json()["timeout_s"] == 3
    assert r.json()["name"] == "web"

    listing = client.get("/api/registry/services/media/probes").json()
    assert [(p["name"], p["kind"]) for p in listing] == [("web", "tcp"), ("unit", "systemd")]
    assert [p["name"] for p in client.get("/api/registry/services/media").json()["probes"]] == [
        "web",
        "unit",
    ]

    assert client.delete(f"/api/registry/probes/{probe['id']}").status_code == 200
    assert [p["name"] for p in client.get("/api/registry/services/media/probes").json()] == ["unit"]
    assert client.delete(f"/api/registry/probes/{probe['id']}").status_code == 404
    actions = _audit_actions()
    assert {"probe.created", "probe.updated", "probe.deleted"} <= set(actions)


def test_test_probe_runs_it_now_and_returns_the_raw_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/registry/probes/{id}/test`` — the single-probe debug
    endpoint. Runs for real (through the same HttpProbe as a scheduled scan)
    and returns state/message/details, without touching ServiceState."""
    node = _make_node(client)
    _make_service(client, node["id"])
    probe = client.post(
        "/api/registry/services/media/probes",
        json={"name": "web", "kind": "http", "config": HTTP_CONFIG},
    ).json()

    class FailingClient:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def __aenter__(self) -> FailingClient:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def request(self, *a: Any, **k: Any) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)

    r = client.post(f"/api/registry/probes/{probe['id']}/test")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["probe_name"] == "web"
    assert result["kind"] == "http"
    assert result["state"] == "unknown"
    assert "connection refused" in result["message"]
    assert result["details"]["indeterminate"] is True

    # Diagnostic only — it must never persist a verdict for the service.
    from orchestrator.db.models import ServiceState

    with Session(get_engine()) as s:
        assert s.exec(select(ServiceState)).first() is None


def test_test_probe_404s_for_a_missing_probe(client: TestClient) -> None:
    assert client.post("/api/registry/probes/999999/test").status_code == 404


def test_test_probe_401s_without_a_session(app: Any) -> None:
    with TestClient(app) as anon:
        r = anon.post("/api/registry/probes/1/test")
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"name": "x", "kind": "telepathy", "config": {}}, "unknown probe kind"),
        ({"name": "x", "kind": "http", "config": {}}, "url"),
        ({"name": "x", "kind": "http", "config": {**HTTP_CONFIG, "bogus": 1}}, "bogus"),
        ({"name": "x", "kind": "tcp", "config": {"host": "h", "port": "not-a-port"}}, "port"),
        ({"name": "x", "kind": "systemd", "config": {"unit": "a.service"}}, "transport"),
        (
            {
                "name": "x",
                "kind": "systemd",
                "config": {"unit": "a.service", "transport": {"type": "ssh"}},
            },
            "transport.host",
        ),
        (
            {
                "name": "x",
                "kind": "systemd",
                "config": {"unit": "a.service", "transport": {"type": "carrier-pigeon"}},
            },
            "transport.type",
        ),
        ({"name": "x", "kind": "http", "config": HTTP_CONFIG, "timeout_s": 0}, "timeout_s"),
    ],
)
def test_probe_create_validation(client: TestClient, body: dict[str, Any], fragment: str) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])
    r = client.post("/api/registry/services/media/probes", json=body)
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]


def test_probe_local_transport_needs_no_host(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])
    r = client.post(
        "/api/registry/services/media/probes",
        json={
            "name": "x",
            "kind": "command",
            "config": {"argv": ["true"], "transport": {"type": "local"}},
        },
    )
    assert r.status_code == 201, r.text


def test_probe_duplicate_name_and_kind_switch_are_checked(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])
    first = client.post(
        "/api/registry/services/media/probes",
        json={"name": "web", "kind": "http", "config": HTTP_CONFIG},
    ).json()
    client.post(
        "/api/registry/services/media/probes",
        json={"name": "unit", "kind": "systemd", "config": SYSTEMD_CONFIG},
    )
    dup = client.post(
        "/api/registry/services/media/probes",
        json={"name": "web", "kind": "http", "config": HTTP_CONFIG},
    )
    assert dup.status_code == 409
    assert client.put(f"/api/registry/probes/{first['id']}", json={"name": "unit"}).status_code == (
        409
    )
    # Changing kind alone re-validates the existing config against the new kind.
    r = client.put(f"/api/registry/probes/{first['id']}", json={"kind": "tcp"})
    assert r.status_code == 400
    assert client.put("/api/registry/probes/999", json={"name": "x"}).status_code == 404


def test_probe_on_unknown_service_404s(client: TestClient) -> None:
    r = client.post(
        "/api/registry/services/ghost/probes",
        json={"name": "web", "kind": "http", "config": HTTP_CONFIG},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Round trip through the existing Save / Reset endpoints
# ---------------------------------------------------------------------------


def test_crud_edits_round_trip_through_save_and_reset(client: TestClient) -> None:
    node = _make_node(client, "pve1", always_on=True)
    _make_service(client, node["id"], slug="gateway")
    _make_service(client, node["id"], slug="media", depends_on=["gateway"])
    client.post(
        "/api/registry/services/media/probes",
        json={"name": "web", "kind": "http", "config": HTTP_CONFIG},
    )
    assert client.get("/api/registry/status").json()["dirty"] is True

    assert client.post("/api/registry/save").status_code == 200
    assert client.get("/api/registry/status").json()["dirty"] is False

    # A live edit after the save is discarded by Reset.
    client.put("/api/registry/services/media", json={"name": "Renamed"})
    assert client.post("/api/registry/reset").status_code == 200
    detail = client.get("/api/registry/services/media").json()
    assert detail["name"] == "Media"
    assert detail["depends_on"] == ["gateway"]
    assert [p["name"] for p in detail["probes"]] == ["web"]
    with Session(get_engine()) as s:
        assert s.exec(select(Node)).one().always_on is True
        assert len(s.exec(select(Service)).all()) == 2


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def test_service_detail_page_renders_for_admin(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])
    client.post(
        "/api/registry/services/media/probes",
        json={"name": "web", "kind": "http", "config": HTTP_CONFIG},
    )
    r = client.get("/services/media")
    assert r.status_code == 200
    assert "Probes" in r.text
    assert "http://10.0.0.5/health" in r.text  # probe config embedded for the drawer
    assert "function probeDrawer" in client.get("/static/service.js").text


def test_service_detail_page_404s_for_unknown_service(client: TestClient) -> None:
    assert client.get("/services/ghost").status_code == 404


def test_dashboard_links_to_detail_and_offers_create_forms(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"])
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="new-service"' in r.text  # opens the service drawer
    assert '"slug": "media"' in r.text  # at a glance, linking to its page
    assert '"/services/" + enc(s.slug)' in client.get("/static/dashboard.js").text
    assert 'href="/sources#proxmox"' in client.get("/nodes").text  # nodes are imported


def test_service_group_round_trips_through_save_and_reset(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], group="media")
    _make_service(client, node["id"], slug="other", group="")

    assert client.post("/api/registry/save").status_code == 200
    assert client.get("/api/registry/status").json()["dirty"] is False

    # A live edit after the save is discarded by Reset, same as any other field.
    client.put("/api/registry/services/media", json={"group": "renamed"})
    assert client.post("/api/registry/reset").status_code == 200
    detail = client.get("/api/registry/services/media").json()
    assert detail["group"] == "media"
    assert client.get("/api/registry/services/other").json()["group"] == ""


def test_dashboard_groups_services_at_a_glance(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], slug="media", group="entertainment")
    _make_service(client, node["id"], slug="cloud", group="entertainment")
    _make_service(client, node["id"], slug="gateway", group="")

    glance = client.get("/api/views/dashboard").json()["glance"]
    groups = {g["name"]: [s["slug"] for s in g["services"]] for g in glance}
    # one box per group; a service without a group sits under its node - never hidden
    assert groups["entertainment"] == ["cloud", "media"]
    assert groups[node["name"]] == ["gateway"]
    # the Services list groups the same way on demand, and filters activity per service
    js = client.get("/static/services.js").text
    assert 'key: "group", label: "Group"' in js


def test_service_detail_page_has_group_field(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], group="media")
    r = client.get("/services/media")
    assert r.status_code == 200
    assert '"group": "media"' in r.text
    assert 'key: "group", label: "Group"' in client.get("/static/service.js").text
