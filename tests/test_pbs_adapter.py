"""PBS row mapping and snapshot identity."""

from __future__ import annotations

from orchestrator.adapters.pbs.api import _snapshot_from_row
from orchestrator.adapters.pbs.base import Snapshot


def test_snapshot_id_renders_epoch_as_iso() -> None:
    snap = Snapshot(
        datastore="store",
        backup_type="vm",
        backup_id="9001",
        backup_time=1756000000,
    )
    assert snap.group == "vm/9001"
    assert snap.snapshot_id.startswith("vm/9001/")
    assert snap.snapshot_id.endswith("Z")


def test_row_mapping_reads_verification_state() -> None:
    verified = _snapshot_from_row(
        "store",
        {
            "backup-type": "vm",
            "backup-id": "9001",
            "backup-time": 1756000000,
            "size": 1234,
            "verification": {"state": "ok"},
            "protected": True,
            "owner": "orchestrator@pbs",
        },
    )
    assert verified.verified is True
    assert verified.protected is True
    assert verified.size_bytes == 1234
    assert verified.owner == "orchestrator@pbs"


def test_row_mapping_treats_missing_verification_as_unverified() -> None:
    snap = _snapshot_from_row(
        "store",
        {"backup-type": "vm", "backup-id": "9001", "backup-time": 1756000000},
    )
    assert snap.verified is False
    assert snap.protected is False
    assert snap.size_bytes is None


def test_row_mapping_treats_failed_verification_as_unverified() -> None:
    snap = _snapshot_from_row(
        "store",
        {
            "backup-type": "vm",
            "backup-id": "9001",
            "backup-time": 1756000000,
            "verification": {"state": "failed"},
        },
    )
    assert snap.verified is False


def test_datastore_status_used_fraction_handles_zero_total() -> None:
    from orchestrator.adapters.pbs.base import DatastoreStatus

    ds = DatastoreStatus(
        name="empty", total_bytes=0, used_bytes=0, available_bytes=0, reachable=True
    )
    assert ds.used_fraction == 0.0
