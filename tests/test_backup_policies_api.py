"""``/api/backup-policies`` CRUD + the ``/backup-policies`` dashboard page.

Auth goes through the same mocked-WebAuthn helpers as ``test_auth.py``. The
app's real lifespan runs (TestClient as a context manager), so the scheduler
is live and these tests also prove an API edit reaches the job set at once.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, BackupPolicy, Node, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.web.app import create_app
from tests.test_auth import _register


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        node = Node(name="eve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        for slug, vmid in (("alpha", 101), ("beta", 102)):
            s.add(
                Service(
                    slug=slug,
                    name=slug.title(),
                    node_id=node.id,
                    guest_kind=GuestKind.VM,
                    guest_id=vmid,
                )
            )
        s.commit()
    return create_app()


def _login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
    assert r.status_code == 200, r.text


NIGHTLY = {
    "name": "nightly",
    "schedule_cron": "0 3 * * *",
    "mode": "snapshot",
    "retention": {"keep_daily": 7, "keep_monthly": 6, "protect_latest_verified": True},
    "services": ["alpha"],
}


def _audit_actions() -> list[str]:
    with Session(get_engine()) as s:
        return [e.action for e in s.exec(select(AuditEntry)).all()]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/backup-policies"),
        ("GET", "/api/backup-policies/nightly"),
        ("POST", "/api/backup-policies"),
        ("PUT", "/api/backup-policies/nightly"),
        ("DELETE", "/api/backup-policies/nightly"),
    ],
)
def test_backup_policy_api_401s_without_a_session(app: Any, method: str, path: str) -> None:
    with TestClient(app) as client:
        r = client.request(method, path, json=NIGHTLY)
    assert r.status_code == 401


def test_crud_round_trip_audits_and_schedules(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)

        r = client.post("/api/backup-policies", json=NIGHTLY)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["retention"] == NIGHTLY["retention"]
        assert body["services"] == ["alpha"]
        assert body["scheduler_running"] is True
        assert body["scheduled"] is True
        assert body["next_run"]

        assert [p["name"] for p in client.get("/api/backup-policies").json()] == ["nightly"]
        assert client.get("/api/backup-policies/nightly").json()["mode"] == "snapshot"

        r = client.put(
            "/api/backup-policies/nightly",
            json={"schedule_cron": "", "services": ["alpha", "beta"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert sorted(body["services"]) == ["alpha", "beta"]
        assert body["retention"] == NIGHTLY["retention"]  # untouched field kept
        assert body["scheduled"] is False and body["next_run"] is None
        assert client.app.state.scheduler.next_run_time("nightly") is None  # type: ignore[attr-defined]

        r = client.delete("/api/backup-policies/nightly")
        assert r.status_code == 200
        assert client.get("/api/backup-policies/nightly").status_code == 404

    actions = _audit_actions()
    for expected in ("backup_policy.created", "backup_policy.updated", "backup_policy.deleted"):
        assert expected in actions
    with Session(get_engine()) as s:
        entry = s.exec(select(AuditEntry).where(AuditEntry.action == "backup_policy.updated")).one()
        assert entry.actor == "storm"
        assert entry.target == "policy:nightly"
        assert entry.details["before"]["schedule_cron"] == "0 3 * * *"
        assert entry.details["after"]["schedule_cron"] == ""


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"name": "has space"}, "name"),
        ({"schedule_cron": "every night"}, "cron"),
        ({"mode": "yolo"}, "mode"),
        ({"retention": {"keep_daily": -1}}, "keep_daily"),
        ({"retention": {"keep_weekly": 2}}, "keep_weekly"),
        ({"services": ["ghost"]}, "ghost"),
        ({"services": "alpha"}, "list"),
        ({"services": ["alpha", "alpha"]}, "more than once"),
    ],
)
def test_create_rejects_bad_input_with_400(
    app: Any, monkeypatch: pytest.MonkeyPatch, patch: dict[str, Any], needle: str
) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.post("/api/backup-policies", json={**NIGHTLY, **patch})
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]


def test_duplicate_404_and_bad_update(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        assert client.post("/api/backup-policies", json=NIGHTLY).status_code == 201
        assert client.post("/api/backup-policies", json=NIGHTLY).status_code == 409
        assert client.put("/api/backup-policies/nope", json={}).status_code == 404
        assert client.delete("/api/backup-policies/nope").status_code == 404
        r = client.put("/api/backup-policies/nightly", json={"retention": {"keep_last": "x"}})
        assert r.status_code == 400
    # The rejected update left the policy as it was.
    with Session(get_engine()) as s:
        pol = s.exec(select(BackupPolicy)).one()
        assert pol.retention == NIGHTLY["retention"]


def test_delete_detaches_services_that_reference_it(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        assert client.post("/api/backup-policies", json=NIGHTLY).status_code == 201
        with Session(get_engine()) as s:
            pol = s.exec(select(BackupPolicy)).one()
            beta = s.exec(select(Service).where(Service.slug == "beta")).one()
            beta.backup_policy_id = pol.id
            s.add(beta)
            s.commit()
        assert sorted(client.get("/api/backup-policies/nightly").json()["services"]) == [
            "alpha",
            "beta",
        ]

        r = client.delete("/api/backup-policies/nightly")
    assert sorted(r.json()["detached_services"]) == ["alpha", "beta"]
    with Session(get_engine()) as s:
        assert s.exec(select(Service).where(Service.slug == "beta")).one().backup_policy_id is None


def test_policy_edits_round_trip_to_yaml_via_registry_save(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy API writes only the DB; the existing Save button does the YAML."""
    yaml_path = get_settings().services_yaml_path
    with TestClient(app) as client:
        _login(client, monkeypatch)
        assert client.post("/api/backup-policies", json=NIGHTLY).status_code == 201
        assert not yaml_path.exists()
        assert client.post("/api/registry/save").status_code == 200

    saved = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    (policy,) = saved["backup_policies"]
    assert policy["name"] == "nightly"
    assert policy["schedule_cron"] == "0 3 * * *"
    assert policy["retention"] == NIGHTLY["retention"]
    # Single source of truth: membership lives on the service, not on the
    # policy's own (legacy) targets list, which dashboard writes always clear.
    (alpha,) = (s for s in saved["services"] if s["slug"] == "alpha")
    assert alpha["backup_policy"] == "nightly"


def test_scheduler_disabled_still_shows_would_be_next_run(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    config_module.reset_settings_cache()
    with TestClient(app) as client:
        assert client.app.state.scheduler is None  # type: ignore[attr-defined]
        _login(client, monkeypatch)
        body = client.post("/api/backup-policies", json=NIGHTLY).json()
        assert body["scheduler_running"] is False
        assert body["scheduled"] is False
        assert body["next_run"]  # computed from the cron, not from a job

        page = client.get("/backup-policies")
    assert "SCHEDULER_ENABLED=false" in page.text


def test_policies_page_redirects_when_not_logged_in(app: Any) -> None:
    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/backup-policies")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_policies_page_renders_policies_and_is_linked_from_dashboard(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        client.post("/api/backup-policies", json=NIGHTLY)
        page = client.get("/backup-policies")
        dashboard = client.get("/")
    assert page.status_code == 200
    assert "nightly" in page.text
    assert "0 3 * * *" in page.text
    assert "keep_daily=7" in page.text
    assert "pins latest verified" in page.text
    assert 'href="/backup-policies"' in dashboard.text


def test_new_policy_page_offers_select_group_buttons(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target-services checklist (on the dedicated add page — see
    docs/adding_a_service.md's Groups section) gets a "select group" button
    per group, and each checkbox carries its service's group as a data
    attribute — the bulk-select JS reads that, the submitted form still
    posts a flat services: [...] list to the existing API."""
    with Session(get_engine()) as s:
        alpha = s.exec(select(Service).where(Service.slug == "alpha")).one()
        alpha.group = "streaming"
        s.add(alpha)
        s.commit()

    with TestClient(app) as client:
        _login(client, monkeypatch)
        page = client.get("/backup-policies/new")

    assert page.status_code == 200
    assert 'data-select-group="streaming"' in page.text
    assert 'data-group="streaming"' in page.text
    # beta has no group — still listed, with an empty data-group, never hidden.
    assert 'value="beta" data-group=""' in page.text


# ---------------------------------------------------------------------------
# Dedicated add/edit pages (same pattern as /proxy-hosts/new + /{id}/edit)
# ---------------------------------------------------------------------------


def test_new_policy_page_renders(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.get("/backup-policies/new")
    assert r.status_code == 200
    assert "New policy" in r.text
    assert "alpha" in r.text and "beta" in r.text


def test_new_policy_page_redirects_when_not_logged_in(app: Any) -> None:
    with TestClient(app, follow_redirects=False) as anon:
        r = anon.get("/backup-policies/new")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/backup-policies/new"


def test_edit_policy_page_renders_prefilled(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        created = client.post("/api/backup-policies", json=NIGHTLY).json()
        assert created["name"] == "nightly"

        r = client.get("/backup-policies/nightly/edit")
    assert r.status_code == 200
    assert "Edit nightly" in r.text
    assert "0 3 * * *" in r.text
    # name field locked on edit (rename = delete + create)
    assert 'id="f-name"' in r.text
    assert "disabled" in r.text
    # the attached service's checkbox is pre-checked
    assert 'value="alpha"' in r.text and "checked" in r.text


def test_edit_policy_page_404s_for_unknown_name(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.get("/backup-policies/does-not-exist/edit")
    assert r.status_code == 404


def test_edit_policy_page_redirects_when_not_logged_in(app: Any) -> None:
    with TestClient(app, follow_redirects=False) as anon:
        r = anon.get("/backup-policies/nightly/edit")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/backup-policies/nightly/edit"


def test_policies_list_page_is_list_only_and_links_to_dedicated_pages(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/backup-policies`` no longer carries the bottom-of-page add/edit
    form — it links out to the dedicated pages instead, same split as
    ``/proxy-hosts``. The per-policy History expander (a different, already
    lazily-loaded feature) is untouched."""
    with TestClient(app) as client:
        _login(client, monkeypatch)
        client.post("/api/backup-policies", json=NIGHTLY)
        page = client.get("/backup-policies")
    assert page.status_code == 200
    assert 'href="/backup-policies/new"' in page.text
    assert 'href="/backup-policies/nightly/edit"' in page.text
    assert 'data-history="' in page.text  # History expander still present
    assert 'id="policy-form"' not in page.text  # inline form is gone
    assert 'id="bp-filter"' in page.text  # name filter box


def test_policies_list_page_filter_box_present_and_filters_by_name(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        client.post("/api/backup-policies", json=NIGHTLY)
        page = client.get("/backup-policies")
    assert page.status_code == 200
    assert 'data-name="nightly"' in page.text


def test_new_policy_page_create_flow(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dedicated add page submits to the same existing API the old
    inline form used — no backend change needed for this feature."""
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.get("/backup-policies/new")
        assert r.status_code == 200
        created = client.post("/api/backup-policies", json=NIGHTLY)
        assert created.status_code == 201
        page = client.get("/backup-policies")
    assert "nightly" in page.text


def test_edit_policy_page_update_flow(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _login(client, monkeypatch)
        client.post("/api/backup-policies", json=NIGHTLY)
        r = client.get("/backup-policies/nightly/edit")
        assert r.status_code == 200
        updated = client.put("/api/backup-policies/nightly", json={"schedule_cron": "0 4 * * *"})
        assert updated.status_code == 200
        assert updated.json()["schedule_cron"] == "0 4 * * *"
