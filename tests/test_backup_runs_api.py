"""``GET /api/runs`` — including the new ``?policy_id=`` filter that backs
the backup-policy run-history view on ``/backup-policies`` (see
docs/roadmap.md's "Backup-policy logs/history view" small feature).

``PipelineRun`` carries ``service_id``, not ``policy_id`` — the filter joins
through ``Service.backup_policy_id``, the single source of truth for policy
membership every other policy-related endpoint already uses
(``backup_policies.py``'s ``_attached_services``). This only has to prove
that join is correct; the pipeline itself is exercised in
``test_backup_pipeline.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator.db import session as session_module
from orchestrator.db.models import BackupPolicy, Node, PipelineRun, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, PipelineKind, PipelineStatus
from orchestrator.web.app import create_app
from tests.test_auth import _register


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
    assert r.status_code == 200, r.text


def _seed() -> dict[str, int]:
    with Session(get_engine()) as s:
        node = Node(name="eve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)

        policy = BackupPolicy(name="nightly")
        s.add(policy)
        s.commit()
        s.refresh(policy)

        covered = Service(
            slug="covered",
            name="Covered",
            node_id=node.id,
            guest_kind=GuestKind.VM,
            guest_id=101,
            backup_policy_id=policy.id,
        )
        uncovered = Service(
            slug="uncovered",
            name="Uncovered",
            node_id=node.id,
            guest_kind=GuestKind.VM,
            guest_id=102,
        )
        s.add(covered)
        s.add(uncovered)
        s.commit()
        s.refresh(covered)
        s.refresh(uncovered)

        s.add(
            PipelineRun(
                kind=PipelineKind.BACKUP,
                correlation_id="run-covered",
                service_id=covered.id,
                status=PipelineStatus.SUCCEEDED,
                started_at=datetime.now(UTC),
            )
        )
        s.add(
            PipelineRun(
                kind=PipelineKind.BACKUP,
                correlation_id="run-uncovered",
                service_id=uncovered.id,
                status=PipelineStatus.FAILED,
                started_at=datetime.now(UTC),
            )
        )
        s.commit()
        return {"policy_id": policy.id, "covered_id": covered.id, "uncovered_id": uncovered.id}


def test_runs_unfiltered_includes_every_service(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.get("/api/runs")
    assert r.status_code == 200, r.text
    correlation_ids = {run["correlation_id"] for run in r.json()}
    assert correlation_ids == {"run-covered", "run-uncovered"}


def test_runs_filtered_by_policy_id_only_includes_covered_services(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.get(f"/api/runs?policy_id={ids['policy_id']}")
    assert r.status_code == 200, r.text
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["correlation_id"] == "run-covered"
    assert runs[0]["service_slug"] == "covered"


def test_runs_filtered_by_policy_id_with_no_runs_is_empty(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.get("/api/runs?policy_id=999999")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_policy_dict_exposes_id_for_the_history_filter(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        r = client.get("/api/backup-policies/nightly")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == ids["policy_id"]


def test_backup_policies_page_shows_each_policys_recent_runs(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed()
    with TestClient(app) as client:
        _login(client, monkeypatch)
        page = client.get("/backup-policies")
        js = client.get("/static/backups.js").text
    assert page.status_code == 200
    assert f'"id": {ids["policy_id"]}' in page.text
    assert '"/api/runs?policy_id=" + p.id' in js
