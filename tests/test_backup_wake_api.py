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


def _required_probe(client: TestClient, slug: str) -> None:
    body = {"name": "web", "kind": "http", "required": True, "config": {"url": "http://10.0.0.9/"}}
    r = client.post(f"/api/registry/services/{slug}/probes", json=body)
    assert r.status_code in (200, 201), r.text


def test_manual_backup_wakes_first_and_fails_clearly_when_it_cannot(client: TestClient) -> None:
    _topology(client)
    _required_probe(client, "app")
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
    _required_probe(client, "app")
    pol = _policy(client, requires=["stuck"])
    client.put("/api/registry/services/app", json={"backup_policy_id": pol["id"]})

    r = client.post("/api/backup/app?wake=false")

    # the probe cannot answer here -> the gate refuses, but the wake phase was skipped, not failed
    assert r.status_code in (400, 409) and "cannot be woken" not in r.json()["detail"]
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
    page = client.get("/backup-policies?new=1")
    assert page.status_code == 200
    html = page.text
    # what can be required, with the services whose node can never be woken flagged
    assert '"requirable": [' in html and '"slug": "truenas"' in html
    assert '"blocker": "node has no power target' in html
    js = client.get("/static/backups.js").text
    assert 'key: "requires", label: "Also needs, for every backup", type: "checklist"' in js
    assert 'key: "wake_before_backup", label: "Wake the services being backed up first"' in js
    # new policy: on by default
    assert "wake_before_backup: editing ? p.wake_before_backup : true" in js


def test_policy_edit_form_is_prefilled(client: TestClient) -> None:
    _topology(client)
    _policy(client, requires=["truenas"], wake_before_backup=False)
    view = client.get("/api/views/backups").json()
    [nightly] = [p for p in view["policies"] if p["name"] == "nightly"]
    assert nightly["requires"] == ["truenas"] and nightly["wake_before_backup"] is False


def test_policies_list_says_what_each_policy_needs(client: TestClient) -> None:
    _topology(client)
    _policy(client, "stuck-pol", requires=["stuck"])
    _policy(client, "calm", requires=["truenas"], wake_before_backup=False)
    view = client.get("/api/views/backups").json()
    by_name = {p["name"]: p for p in view["policies"]}
    assert by_name["calm"]["requires"] == ["truenas"]
    assert [b["slug"] for b in by_name["stuck-pol"]["requires_blockers"]] == ["stuck"]
    js = client.get("/static/backups.js").text
    assert '" cannot be woken"' in js and '"does not wake anything"' in js


def test_service_backups_tab_has_the_plan_panel_and_requirements(client: TestClient) -> None:
    _topology(client)
    html = client.get("/services/app").text
    # What may be woken first is in the page for the Backup drawer's checklist...
    assert '"requirable": [' in html and '"slug": "truenas"' in html
    js = client.get("/static/service.js").text
    assert 'key: "backup_requires", label: "Also wake before its backup", type: "checklist"' in js
    # ...and the Related tab shows the plan and runs the backup, following its steps.
    assert '"/api/backup/plan?slug="' in js and "What a backup wakes first" in js
    assert '"/api/backup/" + enc(slug) + "?verify=true"' in js and "backupSteps" in js


def test_a_service_without_a_required_check_is_skipped_before_anything_is_woken(
    client: TestClient,
) -> None:
    _topology(client)
    pol = _policy(client, requires=["truenas"])
    client.put("/api/registry/services/app", json={"backup_policy_id": pol["id"]})

    r = client.post("/api/backup/app")

    assert r.status_code == 409
    assert (
        r.json()["detail"].startswith("Backup skipped:")
        and "no required check" in r.json()["detail"]
    )
    run = client.get("/api/runs?slug=app&limit=1").json()[0]
    assert run["status"] == "skipped"
    names = [s["name"] for s in run["steps"]]
    assert "checks" in names and not any(n.startswith("wake_") for n in names)
    with Session(get_engine()) as s:
        actions = [e.action for e in s.exec(select(AuditEntry)).all()]
    assert "backup.skipped" in actions
    attention = client.get("/api/views/dashboard").json()["attention"]
    item = next(a for a in attention if a["slug"] == "app")
    assert item["title"].endswith("backup skipped") and "no required check" in item["text"]
    row = next(
        r for r in client.get("/api/views/dashboard").json()["view"]["rows"] if r["slug"] == "app"
    )
    assert row["backup"]["sub"] == "skipped"


def test_the_plan_says_what_would_skip_and_how_long_the_wake_takes(client: TestClient) -> None:
    _topology(client)
    pol = _policy(client, requires=["truenas"])
    client.put("/api/registry/services/app", json={"backup_policy_id": pol["id"]})

    plan = client.get("/api/backup/plan?slug=app").json()

    assert [s["why"] for s in plan["skips"]] == ["no_checks"]
    tl = plan["timeline"]
    assert [s["slug"] for s in tl["steps"]] == ["truenas", "app"]
    truenas = tl["steps"][0]
    assert truenas["steps"][0]["what"].startswith("power storage on")
    assert tl["total_s"] == sum(s["total_s"] for s in tl["steps"]) > 0
    assert tl["learned"] is False
    assert [n["node"] for n in tl["nodes"]] == ["storage"]

    _required_probe(client, "app")
    assert client.get("/api/backup/plan?slug=app").json()["skips"] == []

    # a target on a node nothing can wake is skipped whenever that node is off
    client.put("/api/registry/services/stuck", json={"backup_policy_id": pol["id"]})
    _required_probe(client, "stuck")
    skips = client.get("/api/backup/plan?slug=stuck").json()["skips"]
    assert [s["why"] for s in skips] == ["unwakeable"] and "no power target" in skips[0]["text"]
