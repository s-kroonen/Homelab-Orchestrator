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
    assert {s.slug for s in services} == {
        "example-cloud",
        "example-gateway",
        "example-home",
        "example-media",
        "example-storage",
    }

    # The example demonstrates the gateway-as-blocker pattern: media cannot be
    # judged until the gateway is healthy, because the probe path runs through it.
    media = next(s for s in services if s.slug == "example-media")
    assert media.depends_on == ["example-gateway"]

    # The example demonstrates the circular-backup guard; confirm it loads as a
    # real exclusion rather than just a comment.
    storage = next(s for s in services if s.slug == "example-storage")
    assert storage.enabled is True  # managed...
    assert storage.backup_excluded is True  # ...but never backed up
    assert "circular" in storage.backup_excluded_reason

    nodes = session.exec(select(Node)).all()
    assert {n.name for n in nodes} == {"always-on-gateway", "compute-a", "storage"}

    media = next(s for s in services if s.slug == "example-media")
    probes = session.exec(select(Probe).where(Probe.service_id == media.id)).all()
    assert {p.name for p in probes} == {"http-via-proxy", "docker-project"}


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


def test_replacing_the_entire_node_set_does_not_violate_fk(session: Session) -> None:
    """Regression: swapping the example registry for a real one used to fail with
    "FOREIGN KEY constraint failed" on DELETE FROM node.

    Nodes were deleted before the services referencing them, and
    ``service.node_id`` is ON DELETE RESTRICT. Deletions must run child-first.
    """
    from ruamel.yaml import YAML

    yaml_path = _copy_example(get_settings().services_yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()
    assert {n.name for n in session.exec(select(Node)).all()} == {
        "always-on-gateway",
        "compute-a",
        "storage",
    }

    # Replace every node name AND every service — nothing in common with before.
    yaml = YAML(typ="rt")
    with yaml_path.open("rb") as fh:
        doc = yaml.load(fh)
    doc["nodes"] = [
        {"name": "eve1", "always_on": True, "power_mgr_target": "eve1", "notes": ""},
        {"name": "evepve", "always_on": False, "power_mgr_target": "evepve", "notes": ""},
    ]
    doc["services"] = [
        {
            "slug": "real-thing",
            "name": "Real Thing",
            "description": "",
            "node": "eve1",
            "guest_kind": "vm",
            "guest_id": 100,
            "enabled": True,
            "backup_policy": "daily-frequent",
            "probes": [],
            "proxy_hosts": [],
        }
    ]
    with yaml_path.open("wb") as fh:
        yaml.dump(doc, fh)

    reconcile_yaml_into_db(session, yaml_path)  # must not raise
    session.commit()

    assert {n.name for n in session.exec(select(Node)).all()} == {"eve1", "evepve"}
    assert {s.slug for s in session.exec(select(Service)).all()} == {"real-thing"}


def test_removing_a_node_still_referenced_by_a_kept_service_is_rejected(
    session: Session,
) -> None:
    """The RESTRICT guard must survive the reordering.

    Dropping a node while a service in the SAME file still references it is a
    YAML authoring error and must fail loudly, not silently orphan the service.
    """
    import pytest
    from ruamel.yaml import YAML

    yaml_path = _copy_example(get_settings().services_yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()

    # Drop compute-a but keep the services that sit on it.
    yaml = YAML(typ="rt")
    with yaml_path.open("rb") as fh:
        doc = yaml.load(fh)
    doc["nodes"] = [n for n in doc["nodes"] if n["name"] != "compute-a"]
    with yaml_path.open("wb") as fh:
        yaml.dump(doc, fh)

    # The loader validates references before touching the DB, so this surfaces
    # as a clear authoring error rather than an opaque IntegrityError.
    with pytest.raises(ValueError, match="references node"):
        reconcile_yaml_into_db(session, yaml_path)


def test_backup_excluded_round_trips_through_yaml(session: Session) -> None:
    """Save must not silently drop the exclusion — that would re-enable backups
    for the one service that must never have them."""
    from orchestrator.db.models import Service

    yaml_path = _copy_example(get_settings().services_yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()

    svc = session.exec(select(Service).where(Service.slug == "example-media")).one()
    svc.backup_excluded = True
    svc.backup_excluded_reason = "hosts the PBS datastore"
    session.commit()

    save_registry_to_yaml(session, yaml_path, actor="test")
    session.commit()
    assert compute_registry_diff(session).dirty is False

    # Reload from disk and confirm it survived.
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()
    reloaded = session.exec(select(Service).where(Service.slug == "example-media")).one()
    assert reloaded.backup_excluded is True
    assert reloaded.backup_excluded_reason == "hosts the PBS datastore"
