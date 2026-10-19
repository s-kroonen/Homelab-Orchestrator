"""HTTP side of wake-for-backup: policy/service requirements, the plan preview,
the manual backup's ``wake`` switch, and the pages that render them."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.db.models import AuditEntry
from orchestrator.db.session import get_engine
from tests.test_registry_api import _make_node, _make_service
from tests.test_registry_api import app as app
from tests.test_registry_api import client as client


def _policy(client: TestClient, name: str = "nightly", **extra: Any) -> dict[str, Any]:
    r = client.post("/api/backup-policies", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _topology(client: TestClient) -> dict[str, dict[str, Any]]:
    """storage (off, wakeable) hosts truenas; unwakeable (off, no power target) hosts
    stuck; gw is always on."""
    storage = _make_node(client, "storage", power_mgr_target="storage")
    stuck = _make_node(client, "stuck")  # no power target, not always on
    gw = _make_node(client, "gw", always_on=True)
    return {
        "nodes": {"storage": storage, "stuck": stuck, "gw": gw},
        "truenas": _make_service(client, storage["id"], "truenas"),
        "stuck": _make_service(client, stuck["id"], "stuck"),
        "app": _make_service(client, gw["id"], "app"),
        "db": _make_service(client, gw["id"], "db"),
    }


# ---------------------------------------------------------------------------
# policy API
# ---------------------------------------------------------------------------


def test_policy_defaults_and_round_trip(client: TestClient) -> None:
    _topology(client)
    body = _policy(client, requires=["truenas", "truenas"], wake_before_backup=False)
    assert body["requires"] == ["truenas"]  # duplicates collapsed
    assert body["wake_before_backup"] is False
    assert body["requires_blockers"] == []

    default = _policy(client, "plain")
    assert default["requires"] == [] and default["wake_before_backup"] is True

    r = client.put("/api/backup-policies/plain", json={"requires": ["truenas"]})
    assert r.status_code == 200 and r.json()["requires"] == ["truenas"]
    assert r.json()["wake_before_backup"] is True  # untouched
    r = client.put("/api/backup-policies/plain", json={"wake_before_backup": False})
    assert r.json()["wake_before_backup"] is False and r.json()["requires"] == ["truenas"]
    r = client.put("/api/backup-policies/plain", json={"requires": []})
    assert r.json()["requires"] == []
    assert client.get("/api/backup-policies/nightly").json()["requires"] == ["truenas"]


def test_policy_requires_validation(client: TestClient) -> None:
    _topology(client)
    r = client.post("/api/backup-policies", json={"name": "p", "requires": ["ghost"]})
    assert r.status_code == 400 and "ghost" in r.json()["detail"]
    r = client.post("/api/backup-policies", json={"name": "p", "requires": "truenas"})
    assert r.status_code == 400
    _policy(client, "ok")
    r = client.put("/api/backup-policies/ok", json={"requires": ["ghost"]})
    assert r.status_code == 400
    r = client.put("/api/backup-policies/ok", json={"wake_before_backup": None})
    assert r.status_code == 400
    assert client.get("/api/backup-policies/ok").json()["requires"] == []  # nothing stuck


def test_policy_reports_requirements_whose_node_cannot_be_woken(client: TestClient) -> None:
    _topology(client)
    body = _policy(client, requires=["stuck"])
    assert body["requires_blockers"] == [
        {"slug": "stuck", "node": "stuck", "blocker": "node has no power target — cannot be woken"}
    ]


def test_policy_requires_cycle_is_refused_and_nothing_persists(client: TestClient) -> None:
    topo = _topology(client)
    pol = _policy(client, "loop", services=["app"])
    r = client.put("/api/registry/services/db", json={"backup_requires": ["app"]})
    assert r.status_code == 200
    r = client.put("/api/registry/services/app", json={"backup_requires": ["db"]})
    assert r.status_code == 400 and "cycle" in r.json()["detail"]
    # the policy route also sees the loop: db is covered by "loop" and would require app
    r = client.put("/api/backup-policies/loop", json={"requires": ["db"], "services": ["app"]})
    assert r.status_code == 400 and "cycle" in r.json()["detail"]
    assert client.get("/api/backup-policies/loop").json()["requires"] == []
    assert pol["name"] == "loop" and topo["app"]["slug"] == "app"


# ---------------------------------------------------------------------------
# service API
# ---------------------------------------------------------------------------


def test_service_backup_requires_round_trip_and_validation(client: TestClient) -> None:
    _topology(client)
    r = client.put("/api/registry/services/app", json={"backup_requires": ["db", "db", "truenas"]})
    assert r.status_code == 200, r.text
    assert r.json()["backup_requires"] == ["db", "truenas"]
    assert (
        client.put("/api/registry/services/app", json={"backup_requires": ["ghost"]}).status_code
        == 400
    )
    r = client.put("/api/registry/services/app", json={"backup_requires": ["app"]})
    assert r.status_code == 400 and "itself" in r.json()["detail"]
    r = client.post(
        "/api/registry/services",
        json={"slug": "new", "name": "New", "node_id": 1, "backup_requires": ["truenas"]},
    )
    assert r.status_code == 201 and r.json()["backup_requires"] == ["truenas"]


def test_a_required_service_cannot_be_deleted(client: TestClient) -> None:
    _topology(client)
    _policy(client, requires=["truenas"])
    r = client.delete("/api/registry/services/truenas")
    assert r.status_code == 409 and "policy nightly" in r.json()["detail"]
    client.put("/api/backup-policies/nightly", json={"requires": []})
    client.put("/api/registry/services/app", json={"backup_requires": ["db"]})
    r = client.delete("/api/registry/services/db")
    assert r.status_code == 409 and "app" in r.json()["detail"]


def test_requirements_survive_save_and_reset(client: TestClient) -> None:
    _topology(client)
    _policy(client, requires=["truenas"], wake_before_backup=False)
    client.put("/api/registry/services/app", json={"backup_requires": ["db"]})
    assert client.post("/api/registry/save").status_code == 200
    assert client.post("/api/registry/reset").status_code == 200
    assert client.get("/api/backup-policies/nightly").json()["requires"] == ["truenas"]
    assert client.get("/api/backup-policies/nightly").json()["wake_before_backup"] is False
    assert client.get("/api/registry/services/app").json()["backup_requires"] == ["db"]


# ---------------------------------------------------------------------------
# plan endpoint + manual trigger
# ---------------------------------------------------------------------------


def test_plan_endpoint_is_admin_only(app: Any) -> None:
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/backup/plan?slug=app").status_code == 401


def test_plan_endpoint_lists_ordered_steps_with_blockers(client: TestClient) -> None:
    topo = _topology(client)
    pol = _policy(client, requires=["truenas", "stuck"])
    client.put(
        "/api/registry/services/app",
        json={"backup_policy_id": pol["id"], "backup_requires": ["db"]},
    )

    r = client.get("/api/backup/plan?slug=app")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "app" and body["policy"] == "nightly"
    assert [(s["slug"], s["reason"]) for s in body["steps"]] == [
        ("truenas", "policy prerequisite"),
        ("stuck", "policy prerequisite"),
        ("db", "service prerequisite"),
        ("app", "backup target"),
    ]
    first = body["steps"][0]
    assert first["node"] == "storage" and first["node_power"] == "unknown"
    assert first["blocker"] is None and first["name"] == "Truenas"
    assert body["steps"][2]["node_power"] == "always_on"
    assert body["blockers"] == ["stuck: node has no power target — cannot be woken"]
    assert body["will_wake"] is True
    assert topo["app"]["slug"] == "app"


def test_plan_endpoint_policy_override_and_errors(client: TestClient) -> None:
    _topology(client)
    _policy(client, "storage-backed", requires=["truenas"])
    plain = client.get("/api/backup/plan?slug=app").json()
    assert plain["policy"] is None and [s["slug"] for s in plain["steps"]] == ["app"]
    assert plain["will_wake"] is False  # app sits on an always-on node

    previewed = client.get("/api/backup/plan?slug=app&policy=storage-backed").json()
    assert [s["slug"] for s in previewed["steps"]] == ["truenas", "app"]
    assert client.get("/api/backup/plan?slug=ghost").status_code == 404
    assert client.get("/api/backup/plan?slug=app&policy=ghost").status_code == 404
    assert client.get("/api/backup/plan").status_code == 422


def test_manual_backup_wakes_first_and_fails_clearly_when_it_cannot(client: TestClient) -> None:
    _topology(client)
    pol = _policy(client, requires=["stuck"])
    client.put("/api/registry/services/app", json={"backup_policy_id": pol["id"]})

    r = client.post("/api/backup/app")

    assert r.status_code == 400
    assert "cannot be woken: no power target configured" in r.json()["detail"]
    run = client.get("/api/runs?slug=app&limit=1").json()[0]
    assert run["status"] == "failed"
    step = next(s for s in run["steps"] if s["name"] == "wake_prerequisite:stuck")
    assert step["status"] == "failed" and step["detail"]["node"] == "stuck"
    assert not any(s["name"] in ("pbs_preflight", "integrity_gate") for s in run["steps"])


def test_manual_backup_wake_false_skips_the_wake_and_audits(client: TestClient) -> None:
    _topology(client)
    pol = _policy(client, requires=["stuck"])
    client.put("/api/registry/services/app", json={"backup_policy_id": pol["id"]})

    r = client.post("/api/backup/app?wake=false")

    # no probes -> the gate refuses (400), but the wake phase was skipped, not failed
    assert r.status_code == 400 and "cannot be woken" not in r.json()["detail"]
    run = client.get("/api/runs?slug=app&limit=1").json()[0]
    skipped = next(s for s in run["steps"] if s["name"] == "wake_prerequisites")
    assert skipped["status"] == "skipped"
    assert skipped["detail"]["would_wake"] == ["stuck", "app"]
    with Session(get_engine()) as s:
        actions = [e.action for e in s.exec(select(AuditEntry)).all()]
    assert "backup.wake_skipped" in actions


def test_runs_can_be_filtered_by_slug(client: TestClient) -> None:
    _topology(client)
    client.post("/api/backup/app?wake=false")
    client.post("/api/backup/db?wake=false")
    assert {r["service_slug"] for r in client.get("/api/runs?slug=app").json()} == {"app"}
    assert len(client.get("/api/runs").json()) == 2


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def test_policy_form_offers_the_requirement_picker_and_wake_toggle(client: TestClient) -> None:
    _topology(client)
    page = client.get("/backup-policies/new")
    assert page.status_code == 200
    html = page.text
    assert "Services that must be up for this policy" in html
    assert 'name="req" value="truenas"' in html
    assert "woken" in html and "held awake until the backup finishes" in html
    assert 'id="f-wake-before"' in html and "Wake the services being backed up first" in html
    # new policy: toggle defaults on
    assert 'id="f-wake-before"\n          checked' in html or "checked>\n          Wake" in html
    # a node that cannot be woken is flagged before anyone saves
    assert 'data-blocker="node has no power target' in html
    assert "cannot be woken" in html


def test_policy_edit_form_is_prefilled(client: TestClient) -> None:
    _topology(client)
    _policy(client, requires=["truenas"], wake_before_backup=False)
    html = client.get("/backup-policies/nightly/edit").text
    assert 'name="req" value="truenas"' in html
    truenas_box = html[html.index('name="req" value="truenas"') :].split(">", 1)[0]
    assert "checked" in truenas_box
    toggle = html[html.index('id="f-wake-before"') :].split(">", 1)[0]
    assert "checked" not in toggle


def test_policies_list_has_a_needs_column(client: TestClient) -> None:
    _topology(client)
    _policy(client, "stuck-pol", requires=["stuck"])
    _policy(client, "calm", requires=["truenas"], wake_before_backup=False)
    html = client.get("/backup-policies").text
    assert "<th>Needs</th>" in html
    assert "<code>truenas</code>" in html
    assert "stuck: cannot be woken" in html
    assert "targets not woken" in html


def test_service_backups_tab_has_the_plan_panel_and_requirements(client: TestClient) -> None:
    _topology(client)
    html = client.get("/services/app").text
    assert 'id="backup-plan-panel"' in html and "Backup plan" in html
    assert "/api/backup/plan?slug=app" in html
    assert "Extra requirements for this service" in html
    assert 'id="f-backup-requires"' in html
    assert (
        'value="truenas"' in html
        and 'value="app"' not in html.split('id="f-backup-requires"')[1].split("</div>")[0]
    )
    assert 'id="run-backup-btn"' in html and "Wakes what the backup plan lists first" in html
    assert "backupInFlight" in html  # the 60s reload guard knows about a running backup
    assert "reqFormDirty" in html
