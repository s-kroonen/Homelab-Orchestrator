"""Activity filters (kind / service / guest / node / problems / search) and the
timezone setting."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator import config as config_module
from orchestrator.db import session as session_module
from orchestrator.db.models import (
    AuditEntry,
    Node,
    NotificationEvent,
    PipelineRun,
    Service,
)
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import AuditResult, GuestKind, PipelineKind, PipelineStatus
from orchestrator.web.app import create_app
from orchestrator.web.routers.activity import build_feed
from tests.test_proxy_hosts import _login_as_first_admin

NOW = datetime.now(UTC)


@pytest.fixture
def seeded() -> Any:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        eve = Node(name="eve")
        pve = Node(name="pve")
        s.add(eve)
        s.add(pve)
        s.commit()
        s.refresh(eve)
        s.refresh(pve)
        media = Service(
            slug="media", name="Media", node_id=eve.id, guest_kind=GuestKind.VM, guest_id=101
        )
        s.add(media)
        s.commit()
        s.refresh(media)
        sub = Service(slug="plex", name="Plex", node_id=eve.id, parent_id=media.id)
        cloud = Service(
            slug="cloud", name="Cloud", node_id=pve.id, guest_kind=GuestKind.CT, guest_id=200
        )
        s.add(sub)
        s.add(cloud)
        s.commit()
        s.refresh(cloud)
        s.add(
            PipelineRun(
                kind=PipelineKind.WAKE,
                correlation_id="w1",
                service_id=media.id,
                status=PipelineStatus.FAILED,
                started_at=NOW - timedelta(hours=2),
                error="node never came up",
            )
        )
        s.add(
            PipelineRun(
                kind=PipelineKind.BACKUP,
                correlation_id="b1",
                service_id=cloud.id,
                status=PipelineStatus.SUCCEEDED,
                started_at=NOW - timedelta(hours=1),
            )
        )
        s.add(
            AuditEntry(
                actor="storm",
                action="infra.node_power_action",
                target="node:eve",
                result=AuditResult.OK,
                created_at=NOW - timedelta(minutes=30),
            )
        )
        s.add(
            AuditEntry(
                actor="storm",
                action="auth.login",
                target="admin:1",
                result=AuditResult.OK,
                created_at=NOW - timedelta(minutes=20),
            )
        )
        s.add(
            NotificationEvent(
                at=NOW - timedelta(minutes=10),
                level="node",
                target="eve",
                name="eve",
                event="down",
                severity="error",
                title="eve is DOWN",
            )
        )
        s.add(
            NotificationEvent(
                at=NOW - timedelta(minutes=5),
                level="sub",
                target="plex",
                name="Plex",
                event="failed",
                severity="error",
                title="Plex is FAILED",
            )
        )
        s.commit()


def _titles(events: list[dict[str, Any]]) -> list[str]:
    out = []
    for e in events:
        out.append(
            e.get("title") or e.get("action") or f"{e['kind']}:{e.get('service')}"  # run or audit
        )
    return out


def test_unfiltered_feed_merges_runs_audit_and_alerts(seeded: Any) -> None:
    with Session(get_engine()) as s:
        events = build_feed(s)
    assert {e["type"] for e in events} == {"run", "audit", "alert"}
    assert events[0]["title"] == "Plex is FAILED"  # newest first


def test_kind_filter(seeded: Any) -> None:
    with Session(get_engine()) as s:
        assert {e["type"] for e in build_feed(s, kind="alert")} == {"alert"}
        assert [e["kind"] for e in build_feed(s, kind="wake")] == ["wake"]
        assert {e["type"] for e in build_feed(s, kind="admin")} == {"audit"}


def test_node_filter_covers_its_services_the_node_itself_and_sub_services(seeded: Any) -> None:
    with Session(get_engine()) as s:
        feed = build_feed(s, node="eve")
    titles = _titles(feed)
    assert "wake:media" in titles  # a run on a service on eve
    assert "infra.node_power_action" in titles  # the node's own audit row
    assert "eve is DOWN" in titles  # the node's own alert
    assert "Plex is FAILED" in titles  # a sub-service lives on its root's node
    assert "backup:cloud" not in titles  # pve's
    assert "auth.login" not in titles  # no place: drops out


def test_guest_filter_is_one_guest_and_its_sub_services(seeded: Any) -> None:
    with Session(get_engine()) as s:
        feed = build_feed(s, guest="eve/101")
    assert set(_titles(feed)) == {"wake:media", "Plex is FAILED"}
    with Session(get_engine()) as s:
        assert build_feed(s, guest="pve/101") == []  # right vmid, wrong node


def test_service_filter_and_unknown_service_match_nothing(seeded: Any) -> None:
    with Session(get_engine()) as s:
        assert _titles(build_feed(s, service="cloud")) == ["backup:cloud"]
        assert build_feed(s, service="ghost") == []


def test_problems_only_and_since_and_search(seeded: Any) -> None:
    with Session(get_engine()) as s:
        problems = build_feed(s, problems=True)
        assert {t for t in _titles(problems)} == {"wake:media", "eve is DOWN", "Plex is FAILED"}
        assert _titles(build_feed(s, since_hours=1, kind="wake")) == []
        assert _titles(build_feed(s, q="never came up")) == ["wake:media"]
        assert _titles(build_feed(s, q="power_action")) == ["infra.node_power_action"]


@pytest.fixture
def client(seeded: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_activity_api_and_page_take_the_filters(client: TestClient) -> None:
    r = client.get("/api/activity", params={"node": "eve", "kind": "alert"})
    assert r.status_code == 200
    assert [e["title"] for e in r.json()] == ["Plex is FAILED", "eve is DOWN"]

    page = client.get("/activity", params={"guest": "eve/101", "problems": "true"})
    assert page.status_code == 200
    data = _initial(page.text)
    titles = [e.get("title") for e in data["events"]]
    assert "Plex is FAILED" in titles and "eve is DOWN" not in titles
    assert data["filters"]["guest"] == "eve/101" and data["filters"]["problems"] is True
    assert _initial(client.get("/activity", params={"service": "ghost"}).text)["events"] == []


# -- timezone ----------------------------------------------------------------


def test_timezone_defaults_then_can_be_set_and_is_audited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIMEZONE", "Europe/Amsterdam")
    config_module.reset_settings_cache()
    assert client.get("/api/display").json() == {
        "timezone": "Europe/Amsterdam",
        "default": "Europe/Amsterdam",
    }

    r = client.put("/api/display", json={"timezone": "America/New_York"})
    assert r.status_code == 200 and r.json()["timezone"] == "America/New_York"
    assert client.get("/api/display").json()["timezone"] == "America/New_York"
    assert any(
        e["action"] == "display.timezone" for e in client.get("/api/activity?kind=admin").json()
    )

    assert client.put("/api/display", json={"timezone": "Mars/Olympus"}).status_code == 400
    back = client.put("/api/display", json={"timezone": ""}).json()
    assert back["timezone"] == "Europe/Amsterdam"
    assert "Europe/Amsterdam" in client.get("/api/display/timezones").json()


def test_timezone_moves_the_backup_schedules(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIMEZONE", "UTC")
    config_module.reset_settings_cache()
    scheduler = client.app.state.scheduler  # type: ignore[attr-defined]
    client.put("/api/display", json={"timezone": "Asia/Tokyo"})
    assert str(scheduler.timezone) == "Asia/Tokyo"


def test_display_api_needs_a_login(seeded: Any) -> None:
    with TestClient(create_app()) as anon:
        assert anon.get("/api/display").status_code == 401
        assert anon.put("/api/display", json={"timezone": "UTC"}).status_code == 401


def _initial(html: str) -> dict[str, Any]:
    """The data a page embeds for its first paint."""
    start = html.index('<script type="application/json" id="initial">') + len(
        '<script type="application/json" id="initial">'
    )
    return json.loads(html[start : html.index("</script>", start)])
