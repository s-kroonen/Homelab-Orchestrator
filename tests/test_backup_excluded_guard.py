"""A service that is never backed up (the VM hosting the PBS storage, e.g. TrueNAS) cannot be put
in a backup policy, and a policy never runs it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.cli import cli
from orchestrator.db import session as session_module
from orchestrator.db.models import BackupPolicy, Service
from orchestrator.db.session import get_engine
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.scheduler import resolve_targets
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

REGISTRY = """\
version: 1
nodes:
  - {name: pve1, always_on: true}
backup_policies:
  - {name: nightly, schedule_cron: "0 2 * * *", targets: {services: [media]}}
services:
  - slug: media
    name: media
    node: pve1
    guest_kind: vm
    guest_id: 111
    backup_policy: nightly
  - slug: truenas
    name: TrueNAS
    node: pve1
    guest_kind: vm
    guest_id: 100
    backup_excluded: true
    backup_excluded_reason: hosts the PBS datastore
"""


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    return path


@pytest.fixture
def client(registry: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def _policy_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "weekly", "schedule_cron": "0 3 * * 0", "services": ["media"]}
    body.update(over)
    return body


def test_a_policy_cannot_select_a_never_backed_up_service(client: TestClient) -> None:
    r = client.post("/api/backup-policies", json=_policy_body(services=["media", "truenas"]))
    assert r.status_code == 400, r.text
    assert "truenas" in r.text and "hosts the PBS datastore" in r.text
    assert client.post("/api/backup-policies", json=_policy_body()).status_code == 201
    r = client.put("/api/backup-policies/weekly", json={"services": ["truenas"]})
    assert r.status_code == 400 and "never backed up" in r.text


def test_a_service_cannot_be_given_a_policy_while_excluded(client: TestClient) -> None:
    pid = client.get("/api/backup-policies").json()[0]["id"]
    r = client.put("/api/registry/services/truenas", json={"backup_policy_id": pid})
    assert r.status_code == 400 and "cannot be in a backup policy" in r.text
    # nor both at once
    r = client.put(
        "/api/registry/services/media",
        json={"backup_excluded": True, "backup_excluded_reason": "x", "backup_policy_id": pid},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/registry/services",
        json={"slug": "new", "name": "n", "node_id": 1, "backup_excluded": True,
              "backup_excluded_reason": "x", "backup_policy_id": pid},
    )  # fmt: skip
    assert r.status_code == 400


def test_excluding_a_service_takes_it_out_of_its_policy(client: TestClient) -> None:
    r = client.put(
        "/api/registry/services/media",
        json={"backup_excluded": True, "backup_excluded_reason": "now hosts the datastore"},
    )
    assert r.status_code == 200, r.text
    with Session(get_engine()) as s:
        assert s.exec(select(Service).where(Service.slug == "media")).one().backup_policy_id is None


def test_the_bulk_set_policy_action_is_refused_for_it_too(client: TestClient) -> None:
    pid = client.get("/api/backup-policies").json()[0]["id"]
    r = client.post(
        "/api/bulk/services",
        json={"action": "update", "slugs": ["truenas"], "params": {"backup_policy_id": pid}},
    )
    assert r.status_code == 200
    item = r.json()["results"][0]
    assert item["ok"] is False and "backup policy" in item["message"]


def test_a_policy_never_resolves_it_even_if_the_file_still_names_it(registry: Path) -> None:
    with Session(get_engine()) as s:
        pol = s.exec(select(BackupPolicy)).one()
        pol.targets = {"services": ["media", "truenas"]}
        truenas = s.exec(select(Service).where(Service.slug == "truenas")).one()
        truenas.backup_policy_id = pol.id  # contradictory data, as an old file could leave it
        s.add_all([pol, truenas])
        s.commit()
        assert resolve_targets(s, pol) == ["media"]


def test_a_file_that_contradicts_loads_but_the_editor_refuses_to_save_it(
    registry: Path,
) -> None:
    registry.write_text(
        REGISTRY.replace("    guest_id: 100\n", "    guest_id: 100\n    backup_policy: nightly\n")
    )
    with Session(get_engine()) as s:  # an older file still loads: the exclusion wins
        reconcile_yaml_into_db(s, registry)
        s.commit()
        truenas = s.exec(select(Service).where(Service.slug == "truenas")).one()
        assert truenas.backup_policy_id is None
    out = CliRunner().invoke(cli, ["service", "update", "media", "--description", "x"])
    assert out.exit_code != 0 and "cannot be in a policy" in out.output


def test_cli_cannot_attach_a_policy_to_it(registry: Path) -> None:
    out = CliRunner().invoke(cli, ["service", "update", "truenas", "--policy", "nightly"])
    assert out.exit_code != 0 and "backup_excluded" in out.output


def test_the_forms_show_it_but_will_not_let_you_pick_it(client: TestClient) -> None:
    view = client.get("/api/views/backups").json()
    by = {s["slug"]: s for s in view["services"]}
    assert by["truenas"]["excluded"] is True and by["media"]["excluded"] is False
    assert any(x["slug"] == "truenas" for x in view["excluded"])  # "Not backed up: never"
    js = client.get("/static/backups.js").text
    # the policy drawer does not offer it
    assert "V.services.filter((s) => !s.excluded)" in js
