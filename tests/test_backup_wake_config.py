"""Config side of wake-for-backup: the YAML round trip, validation, the migration,
and the ``orchestrator-cli`` commands."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from alembic import command
from alembic.config import Config
from click.testing import CliRunner
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

from orchestrator.cli import cli
from orchestrator.config import get_settings
from orchestrator.db.models import BackupPolicy, Service
from orchestrator.registry.loader import (
    compute_registry_diff,
    reconcile_yaml_into_db,
    save_registry_to_yaml,
)

EXAMPLE = Path(__file__).parent.parent / "config" / "services.example.yaml"


def _load_example(session: Session) -> Path:
    path = get_settings().services_yaml_path
    shutil.copy(EXAMPLE, path)
    reconcile_yaml_into_db(session, path)
    session.commit()
    return path


# ---------------------------------------------------------------------------
# loader: reconcile, round trip, validation
# ---------------------------------------------------------------------------


def test_example_declares_the_storage_vm_as_a_policy_prerequisite(session: Session) -> None:
    _load_example(session)
    for name in ("daily-frequent", "monthly-full"):
        pol = session.exec(select(BackupPolicy).where(BackupPolicy.name == name)).one()
        assert pol.requires == ["example-storage"]
        assert pol.wake_before_backup is True
    # the required service is backup_excluded — expected, and not an error
    storage = session.exec(select(Service).where(Service.slug == "example-storage")).one()
    assert storage.backup_excluded is True


def test_save_round_trip_keeps_the_new_fields(session: Session) -> None:
    path = _load_example(session)
    pol = session.exec(select(BackupPolicy).where(BackupPolicy.name == "daily-frequent")).one()
    pol.wake_before_backup = False
    session.add(pol)
    cloud = session.exec(select(Service).where(Service.slug == "example-cloud")).one()
    cloud.backup_requires = ["example-media"]
    session.add(cloud)
    session.commit()
    assert compute_registry_diff(session).dirty is True

    save_registry_to_yaml(session, path)
    session.commit()

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    policy = next(p for p in doc["backup_policies"] if p["name"] == "daily-frequent")
    assert policy["requires"] == ["example-storage"] and policy["wake_before_backup"] is False
    svc = next(s for s in doc["services"] if s["slug"] == "example-cloud")
    assert svc["backup_requires"] == ["example-media"]
    assert compute_registry_diff(session).dirty is False
    # and it reloads to the same live state
    reconcile_yaml_into_db(session, path)
    session.commit()
    session.expire_all()
    assert session.exec(
        select(Service).where(Service.slug == "example-cloud")
    ).one().backup_requires == ["example-media"]


def _edited(tmp: Path, mutate: Any) -> Path:
    doc = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    mutate(doc)
    out = tmp / "edited.yaml"
    out.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return out


def _service(doc: dict[str, Any], slug: str) -> dict[str, Any]:
    return next(s for s in doc["services"] if s["slug"] == slug)


def test_unknown_slug_in_policy_requires_is_refused(session: Session, tmp_path: Path) -> None:
    path = _edited(tmp_path, lambda d: d["backup_policies"][0].update(requires=["nope"]))
    with pytest.raises(ValueError, match="requires 'nope'"):
        reconcile_yaml_into_db(session, path)


def test_unknown_slug_in_backup_requires_is_refused(session: Session, tmp_path: Path) -> None:
    path = _edited(
        tmp_path, lambda d: _service(d, "example-cloud").update(backup_requires=["nope"])
    )
    with pytest.raises(ValueError, match="backup_requires 'nope'"):
        reconcile_yaml_into_db(session, path)


def test_self_reference_is_refused(session: Session, tmp_path: Path) -> None:
    path = _edited(
        tmp_path, lambda d: _service(d, "example-cloud").update(backup_requires=["example-cloud"])
    )
    with pytest.raises(ValueError, match="lists itself in backup_requires"):
        reconcile_yaml_into_db(session, path)


def test_cycle_through_requires_is_refused(session: Session, tmp_path: Path) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        _service(doc, "example-cloud").update(backup_requires=["example-home"])
        _service(doc, "example-home").update(backup_requires=["example-cloud"])

    with pytest.raises(ValueError, match="form a cycle"):
        reconcile_yaml_into_db(session, _edited(tmp_path, mutate))


def test_cycle_through_the_policy_is_refused(session: Session, tmp_path: Path) -> None:
    # media is backed up under daily-frequent, which requires cloud; cloud requires media.
    def mutate(doc: dict[str, Any]) -> None:
        doc["backup_policies"][0]["requires"] = ["example-cloud"]
        _service(doc, "example-cloud").update(backup_requires=["example-media"])
        _service(doc, "example-media").update(backup_policy="daily-frequent")

    with pytest.raises(ValueError, match="form a cycle"):
        reconcile_yaml_into_db(session, _edited(tmp_path, mutate))


def test_duplicates_collapse_and_depends_on_is_independent(
    session: Session, tmp_path: Path
) -> None:
    def mutate(doc: dict[str, Any]) -> None:
        _service(doc, "example-cloud").update(
            backup_requires=["example-media", "example-media", " example-home "]
        )

    reconcile_yaml_into_db(session, _edited(tmp_path, mutate))
    cloud = session.exec(select(Service).where(Service.slug == "example-cloud")).one()
    assert cloud.backup_requires == ["example-media", "example-home"]
    assert cloud.depends_on == []  # a different thing entirely


# ---------------------------------------------------------------------------
# migration 0010
# ---------------------------------------------------------------------------


def _cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def _insert_row(conn: Any, table: str, values: dict[str, Any]) -> None:
    """Insert one row, filling any other NOT NULL column that has no default."""
    cols = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    row = dict(values)
    for _cid, name, ctype, notnull, default, _pk in cols:
        if name in row or name == "id" or not notnull or default is not None:
            continue
        kind = (ctype or "").upper()
        if "INT" in kind or "BOOL" in kind:
            row[name] = 1
        elif "JSON" in kind:
            row[name] = "{}"
        elif "DATE" in kind or "TIME" in kind:
            row[name] = "2026-01-01 00:00:00"
        else:
            row[name] = "x"
    names = ", ".join(row)
    marks = ", ".join(f":{k}" for k in row)
    conn.execute(text(f"INSERT INTO {table} ({names}) VALUES ({marks})"), row)


def test_migration_applies_on_a_fresh_database_and_reverses() -> None:
    cfg = _cfg()
    command.upgrade(cfg, "head")
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        pol = {r[1] for r in conn.execute(text("PRAGMA table_info(backup_policy)")).fetchall()}
        svc = {r[1] for r in conn.execute(text("PRAGMA table_info(service)")).fetchall()}
    assert {"requires", "wake_before_backup"} <= pol
    assert "backup_requires" in svc
    command.downgrade(cfg, "0009_traffic_rollup")
    with engine.connect() as conn:
        pol = {r[1] for r in conn.execute(text("PRAGMA table_info(backup_policy)")).fetchall()}
    assert "requires" not in pol and "wake_before_backup" not in pol


def test_migration_gives_existing_rows_safe_defaults() -> None:
    cfg = _cfg()
    command.upgrade(cfg, "0009_traffic_rollup")
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        _insert_row(conn, "backup_policy", {"name": "old-policy"})
        _insert_row(conn, "node", {"name": "n1"})
        node_id = conn.execute(text("SELECT id FROM node")).scalar_one()
        _insert_row(conn, "service", {"slug": "old-svc", "name": "Old", "node_id": node_id})

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        requires, wake = conn.execute(
            text("SELECT requires, wake_before_backup FROM backup_policy")
        ).one()
        backup_requires = conn.execute(text("SELECT backup_requires FROM service")).scalar_one()
    assert json.loads(requires) == []
    assert bool(wake) is True  # existing policies pick up wake-before-backup
    assert json.loads(backup_requires) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SAMPLE = """\
# my homelab
version: 1
nodes:
  - name: gw
    always_on: true
    power_mgr_target: gw
    notes: ""
  - name: storage
    always_on: false
    power_mgr_target: storage
    notes: ""
  - name: lonely
    always_on: false
    power_mgr_target: ""
    notes: ""
backup_policies:
  - name: nightly  # the nightly one
    schedule_cron: "0 3 * * *"
    mode: snapshot
    retention: {keep_daily: 7}
    targets: {}
services:
  - slug: truenas
    name: TrueNAS
    node: storage
    guest_kind: vm
    guest_id: 100
    backup_excluded: true
    backup_excluded_reason: hosts the pool
    probes: []
    proxy_hosts: []
  - slug: orphan
    name: Orphan
    node: lonely
    guest_kind: vm
    guest_id: 101
    probes: []
    proxy_hosts: []
  - slug: app
    name: App
    node: gw
    guest_kind: vm
    guest_id: 102
    backup_policy: nightly
    probes:
      - {name: p, kind: tcp, required: true, config: {host: 1.2.3.4, port: 80}}
    proxy_hosts: []
"""


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from orchestrator.db import session as session_module

    session_module.create_all_for_tests()  # the DB commands read the reconciled registry
    path = get_settings().services_yaml_path
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args))


def _doc(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_policy_update_sets_and_clears_requires_preserving_comments(registry: Path) -> None:
    result = _run("policy", "update", "nightly", "--requires", "truenas", "--no-wake-before-backup")
    assert result.exit_code == 0, result.output
    text_ = registry.read_text(encoding="utf-8")
    assert "# my homelab" in text_ and "# the nightly one" in text_
    pol = _doc(registry)["backup_policies"][0]
    assert pol["requires"] == ["truenas"] and pol["wake_before_backup"] is False

    result = _run("policy", "update", "nightly", "--clear-requires", "--wake-before-backup")
    assert result.exit_code == 0, result.output
    pol = _doc(registry)["backup_policies"][0]
    assert pol["requires"] == [] and pol["wake_before_backup"] is True


def test_policy_update_validates_before_writing(registry: Path) -> None:
    before = registry.read_text(encoding="utf-8")
    result = _run("policy", "update", "nightly", "--requires", "ghost")
    assert result.exit_code != 0
    assert "ghost" in result.output
    assert registry.read_text(encoding="utf-8") == before  # untouched


def test_policy_update_unknown_policy_and_no_options(registry: Path) -> None:
    assert _run("policy", "update", "nope", "--requires", "truenas").exit_code != 0
    assert _run("policy", "update", "nightly").exit_code != 0


def test_service_update_sets_backup_requires(registry: Path) -> None:
    result = _run("service", "update", "app", "--backup-requires", "truenas,orphan,truenas")
    assert result.exit_code == 0, result.output
    app = next(s for s in _doc(registry)["services"] if s["slug"] == "app")
    assert app["backup_requires"] == ["truenas", "orphan"]
    result = _run("service", "update", "app", "--backup-requires", "app")
    assert result.exit_code != 0 and "itself" in result.output
    assert _run("service", "update", "app", "--backup-requires", "").exit_code == 0
    app = next(s for s in _doc(registry)["services"] if s["slug"] == "app")
    assert app["backup_requires"] == []


def test_policy_and_service_lists_show_needs_and_unwakeable(registry: Path) -> None:
    assert _run("policy", "update", "nightly", "--requires", "orphan").exit_code == 0
    out = _run("policy", "list").output
    assert "nightly" in out and "orphan" in out and "cannot wake" in out
    out = _run("service", "list").output
    assert "NEEDS" in out
    app_line = next(line for line in out.splitlines() if line.startswith("app"))
    assert "orphan" in app_line and "blocked" in app_line


def test_backup_plan_prints_the_ordered_plan(registry: Path) -> None:
    assert _run("policy", "update", "nightly", "--requires", "truenas").exit_code == 0

    result = _run("backup", "plan", "app")

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip().startswith(("1.", "2."))]
    assert "truenas" in lines[0] and "policy prerequisite" in lines[0]
    assert "app" in lines[1] and "backup target" in lines[1]


def test_backup_plan_exits_nonzero_on_a_blocker(registry: Path) -> None:
    assert _run("policy", "update", "nightly", "--requires", "orphan").exit_code == 0

    result = _run("backup", "plan", "app")

    assert result.exit_code == 1
    assert "BLOCKER" in result.output and "no power target" in result.output


def test_backup_plan_policy_override_and_unknowns(registry: Path) -> None:
    assert _run("policy", "update", "nightly", "--requires", "orphan").exit_code == 0
    # previewing under a policy name that does not exist
    assert _run("backup", "plan", "app", "--policy", "nope").exit_code != 0
    assert _run("backup", "plan", "ghost").exit_code != 0


def test_backup_command_keeps_working_and_accepts_no_wake(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `backup SLUG` (no subcommand) still routes to the run command; in DRY_RUN the
    # gate refuses an unreachable service, which is a clean non-zero exit — not a usage error.
    result = _run("backup", "--no-wake", "--no-gate", "app")
    assert "Usage:" not in result.output
    assert "wake" in result.output.lower()  # the --no-wake warning banner
    help_ = _run("backup", "run", "--help")
    assert "--no-wake" in help_.output
