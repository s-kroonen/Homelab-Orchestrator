"""Activity timeline: the merged run+audit feed, its page, and the dashboard's
per-probe "why is it unhealthy" expansion.

Rows are seeded directly rather than produced by running a pipeline: what is
under test is how the feed merges and folds them, and fixed timestamps make
the ordering assertions exact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import webauthn
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, Node, PipelineRun, Service, ServiceState
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import (
    AuditResult,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


# -- auth helpers, as in test_auth.py -----------------------------------------


def _credential(raw_id: bytes) -> dict[str, Any]:
    b64 = webauthn.helpers.bytes_to_base64url(raw_id)
    return {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}}


def _register(client: TestClient, monkeypatch: pytest.MonkeyPatch, username: str = "storm") -> Any:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=b"cred-1", credential_public_key=b"pub", sign_count=0, aaguid=""
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)
    r = client.post("/api/auth/register/begin", json={"username": username})
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/auth/register/complete",
        json={"username": username, "device_label": "t", "credential": _credential(b"cred-1")},
    )
    assert r.status_code == 200, r.text
    return r


# -- seed data ---------------------------------------------------------------


def _seed() -> None:
    """Two services; a failed wake (with its 2 audit rows) on one, a successful
    backup on the other, and one service-less admin action in between."""
    with Session(get_engine()) as s:
        node = Node(name="eve1")
        s.add(node)
        s.commit()
        s.refresh(node)
        media = Service(
            slug="media", name="Media", node_id=node.id, guest_kind=GuestKind.VM, group="infra"
        )
        wiki = Service(slug="wiki", name="Wiki", node_id=node.id, guest_kind=GuestKind.CT)
        s.add_all([media, wiki])
        s.commit()
        s.refresh(media)
        s.refresh(wiki)

        s.add(
            PipelineRun(
                kind=PipelineKind.WAKE,
                correlation_id="cid-wake",
                service_id=media.id,
                status=PipelineStatus.FAILED,
                started_at=T0,
                finished_at=T0 + timedelta(minutes=5),
                error="node eve1 did not come online",
                steps=[
                    {"name": "hold_asserted", "status": "ok", "detail": {}},
                    {"name": "node_online", "status": "failed", "detail": {"waited_s": 300}},
                ],
            )
        )
        s.add(
            AuditEntry(
                actor="maintenance-page",
                action="wake.start",
                target="service:media",
                correlation_id="cid-wake",
                created_at=T0,
            )
        )
        s.add(
            AuditEntry(
                actor="maintenance-page",
                action="wake.failed",
                target="service:media",
                correlation_id="cid-wake",
                result=AuditResult.ERROR,
                created_at=T0 + timedelta(minutes=5),
            )
        )
        s.add(
            AuditEntry(
                actor="storm",
                action="auth.invite.created",
                correlation_id="cid-invite",
                created_at=T0 + timedelta(minutes=10),
            )
        )
        s.add(
            PipelineRun(
                kind=PipelineKind.BACKUP,
                correlation_id="cid-backup",
                service_id=wiki.id,
                status=PipelineStatus.SUCCEEDED,
                started_at=T0 + timedelta(minutes=20),
                finished_at=T0 + timedelta(minutes=30),
                steps=[{"name": "integrity_gate", "status": "ok", "detail": {}}],
            )
        )
        s.add(
            AuditEntry(
                actor="scheduler",
                action="backup.start",
                target="service:wiki",
                correlation_id="cid-backup",
                created_at=T0 + timedelta(minutes=20),
            )
        )
        s.add(
            ServiceState(
                service_id=media.id,
                last_verdict=HealthState.FAILED,
                last_verdict_at=T0,
                last_probe_results=[
                    {
                        "probe_name": "web-up",
                        "kind": "http",
                        "state": "failed",
                        "latency_ms": 12,
                        "message": "HTTP 502 from upstream",
                        "details": {},
                    }
                ],
            )
        )
        s.commit()


def _feed_from_test_registration(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Registering the test admin itself writes an auth.register row — drop it
    so assertions only see the seeded events."""
    return [e for e in events if e.get("action") != "auth.register"]


# -- route protection -------------------------------------------------------


def test_activity_api_requires_login(app: Any) -> None:
    with TestClient(app) as client:
        assert client.get("/api/activity").status_code == 401


def test_activity_page_redirects_to_login(app: Any) -> None:
    with TestClient(app) as client:
        r = client.get("/activity", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/activity"


# -- the feed ----------------------------------------------------------------


def test_feed_merges_runs_and_audits_newest_first(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch)
        _seed()
        r = client.get("/api/activity")

    assert r.status_code == 200, r.text
    events = _feed_from_test_registration(r.json())
    assert [(e["type"], e.get("kind") or e.get("action")) for e in events] == [
        ("run", "backup"),
        ("audit", "auth.invite.created"),
        ("run", "wake"),
    ]

    wake = events[2]
    # Its two audit rows are folded in, not listed as separate events…
    assert [a["action"] for a in wake["audit"]] == ["wake.start", "wake.failed"]
    # …and the first of them is what says who triggered the run.
    assert wake["actor"] == "maintenance-page"
    assert wake["service"] == "media"
    assert wake["status"] == "failed"
    assert wake["error"] == "node eve1 did not come online"
    assert [s["name"] for s in wake["steps"]] == ["hold_asserted", "node_online"]


def test_feed_filters_by_service(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch)
        _seed()
        media = client.get("/api/activity", params={"service": "media"}).json()
        nobody = client.get("/api/activity", params={"service": "no-such-service"}).json()

    # Only the wake; the admin action has no service target so drops out too.
    assert [(e["type"], e["kind"]) for e in media] == [("run", "wake")]
    # An unknown slug matches nothing — not "every run without a service".
    assert nobody == []


def test_feed_filters_by_group(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch)
        _seed()
        infra = client.get("/api/activity", params={"group": "infra"}).json()
        nobody = client.get("/api/activity", params={"group": "no-such-group"}).json()

    # Only media's wake — media is the only service tagged "infra"; the admin
    # action and wiki's backup both drop out.
    assert [(e["type"], e["kind"]) for e in infra] == [("run", "wake")]
    assert nobody == []


def test_feed_respects_limit(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch)
        _seed()
        r = client.get("/api/activity", params={"limit": 1})
        too_many = client.get("/api/activity", params={"limit": 1000})

    assert len(r.json()) == 1
    assert too_many.status_code == 422  # capped at MAX_EVENTS


# -- pages -------------------------------------------------------------------


def test_activity_page_renders_runs_with_steps(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch)
        _seed()
        r = client.get("/activity")
        filtered = client.get("/activity", params={"service": "wiki"})

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # The step list reuses maintenance.html's classes, so ✓/✗ come from the same CSS.
    assert '<ol class="steps">' in r.text
    assert '<li class="failed">node_online' in r.text
    assert "waited_s=300" in r.text
    assert "node eve1 did not come online" in r.text
    assert "auth.invite.created" in r.text
    assert "by maintenance-page" in r.text

    assert "integrity_gate" in filtered.text
    assert "node_online" not in filtered.text


def test_activity_page_offers_and_applies_group_filter(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch)
        _seed()
        r = client.get("/activity")
        filtered = client.get("/activity", params={"group": "infra"})

    assert 'name="group"' in r.text
    assert '<option value="infra"' in r.text
    assert "node_online" in filtered.text
    assert "integrity_gate" not in filtered.text


def test_dashboard_explains_an_unhealthy_badge(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch)
        _seed()
        r = client.get("/")

    assert r.status_code == 200
    assert 'href="/activity"' in r.text  # nav link
    assert '<li class="failed">web-up' in r.text
    assert "HTTP 502 from upstream" in r.text
    assert 'href="/activity?service=media"' in r.text
