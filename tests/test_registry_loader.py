"""YAML <-> DB reconcile round-trip + dirty detection.

These tests exercise the whole contract the user asked for:
  * boot reconcile: YAML -> DB
  * live edits: DB drift raises ``dirty``
  * Save: DB -> YAML clears ``dirty``
  * Reset: YAML wins again, drops live edits
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlmodel import Session, select

from orchestrator.config import get_settings
from orchestrator.db.models import Node, Probe, Service
from orchestrator.registry.loader import (
    compute_registry_diff,
    reconcile_yaml_into_db,
    reload_registry_from_yaml,
    save_registry_to_yaml,
)

EXAMPLE = Path(__file__).parent.parent / "config" / "services.example.yaml"


def _copy_example(dest: Path) -> Path:
    shutil.copy(EXAMPLE, dest)
    return dest


def test_reconcile_loads_all_entities(session: Session) -> None:
    yaml_path = _copy_example(get_settings().services_yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()

    services = session.exec(select(Service).order_by(Service.slug)).all()
    assert {s.slug for s in services} == {"example-cloud", "example-home", "example-media"}

    nodes = session.exec(select(Node)).all()
    assert {n.name for n in nodes} == {"always-on-gateway", "compute-a", "storage"}

    media = next(s for s in services if s.slug == "example-media")
    probes = session.exec(select(Probe).where(Probe.service_id == media.id)).all()
    assert {p.name for p in probes} == {"http-frontend", "docker-project"}


def test_dirty_flag_flips_after_edit_and_clears_after_save(session: Session) -> None:
    yaml_path = _copy_example(get_settings().services_yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()

    assert compute_registry_diff(session).dirty is False

    # Live edit: flip a service disabled.
    svc = session.exec(select(Service).where(Service.slug == "example-media")).one()
    svc.enabled = False
    session.add(svc)
    session.commit()

    assert compute_registry_diff(session).dirty is True

    save_registry_to_yaml(session, yaml_path, actor="test")
    session.commit()

    assert compute_registry_diff(session).dirty is False

    # Round-trip check: reload the file and confirm the edit persisted to disk.
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()
    svc2 = session.exec(select(Service).where(Service.slug == "example-media")).one()
    assert svc2.enabled is False


def test_reset_discards_live_edits(session: Session) -> None:
    yaml_path = _copy_example(get_settings().services_yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()

    svc = session.exec(select(Service).where(Service.slug == "example-media")).one()
    svc.enabled = False
    session.commit()
    assert compute_registry_diff(session).dirty is True

    reload_registry_from_yaml(session, yaml_path)
    session.commit()

    svc2 = session.exec(select(Service).where(Service.slug == "example-media")).one()
    assert svc2.enabled is True
    assert compute_registry_diff(session).dirty is False


def test_removing_service_from_yaml_deletes_it_but_preserves_backup_history(
    session: Session,
) -> None:
    from orchestrator.db.models import BackupRecord

    yaml_path = _copy_example(get_settings().services_yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()

    media = session.exec(select(Service).where(Service.slug == "example-media")).one()
    session.add(
        BackupRecord(
            service_id=media.id,
            service_slug="example-media",
            pbs_snapshot_id="vm/9001/2026-08-24T02:00:00Z",
        )
    )
    session.commit()

    # Simulate removing the service from YAML: drop the entry, resave, reload.
    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    with yaml_path.open("rb") as fh:
        doc = yaml.load(fh)
    doc["services"] = [s for s in doc["services"] if s["slug"] != "example-media"]
    with yaml_path.open("wb") as fh:
        yaml.dump(doc, fh)

    reconcile_yaml_into_db(session, yaml_path)
    session.commit()

    assert (
        session.exec(select(Service).where(Service.slug == "example-media")).one_or_none() is None
    )

    surviving = session.exec(
        select(BackupRecord).where(BackupRecord.service_slug == "example-media")
    ).all()
    assert len(surviving) == 1
    assert surviving[0].service_id is None  # FK went to SET NULL, row preserved
