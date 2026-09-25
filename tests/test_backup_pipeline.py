"""Backup pipeline behaviour, driven entirely through the dry-run adapters."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterUnreachable
from orchestrator.adapters.pbs.base import DatastoreStatus, Snapshot
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.config import get_settings
from orchestrator.db.models import AuditEntry, BackupRecord, PipelineRun
from orchestrator.domain.enums import HealthState, PipelineStatus
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.pipelines.backup import BackupError, BackupPipeline
from orchestrator.registry.loader import reconcile_yaml_into_db

EXAMPLE = Path(__file__).parent.parent / "config" / "services.example.yaml"


@pytest.fixture
def loaded_session(session: Session) -> Session:
    yaml_path = get_settings().services_yaml_path
    shutil.copy(EXAMPLE, yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()
    return session


def _snapshot(backup_id: str = "9001", when: int = 1756000000) -> Snapshot:
    return Snapshot(
        datastore=get_settings().pbs_datastore,
        backup_type="vm",
        backup_id=backup_id,
        backup_time=when,
        size_bytes=4096,
    )


class StubHealthEngine:
    """Returns a fixed verdict without touching the network.

    The pipeline tests below are about backup mechanics, not about the health
    engine — its own behaviour is covered in test_health_engine.py. Without this
    they would make real HTTP calls to the example config's fake hostnames and
    (correctly) be refused by the gate.
    """

    def __init__(self, state: HealthState = HealthState.HEALTHY, reason: str = "stub") -> None:
        self.state = state
        self.reason = reason
        self.scanned: list[str] = []

    async def scan(self, session: Session, service) -> ServiceVerdict:
        self.scanned.append(service.slug)
        return ServiceVerdict(state=self.state, reason=self.reason, probe_results=[])


def _pipeline(
    proxmox: DryRunProxmoxAdapter,
    pbs: DryRunPbsAdapter,
    *,
    health: StubHealthEngine | None = None,
) -> BackupPipeline:
    return BackupPipeline(
        proxmox=proxmox,
        pbs=pbs,
        settings=get_settings(),
        health=health or StubHealthEngine(),  # type: ignore[arg-type]
    )


async def test_successful_backup_records_and_verifies(loaded_session: Session) -> None:
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    run = await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(
        loaded_session, "example-media"
    )

    assert run.status is PipelineStatus.SUCCEEDED
    assert run.finished_at is not None

    record = loaded_session.exec(
        select(BackupRecord).where(BackupRecord.service_slug == "example-media")
    ).one()
    assert record.verified is True
    assert record.verified_at is not None
    assert record.pbs_snapshot_id.startswith("vm/9001/")
    assert record.pipeline_run_id == run.id


async def test_steps_are_recorded_in_order(loaded_session: Session) -> None:
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    run = await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(
        loaded_session, "example-media"
    )
    names = [s["name"] for s in run.steps]
    assert names == [
        "pbs_preflight",
        "integrity_gate",
        "vzdump_started",
        "vzdump_finished",
        "snapshot_located",
        "backup_recorded",
        "pbs_verify",
    ]


async def test_gate_passes_and_records_the_verdict(loaded_session: Session) -> None:
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    run = await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(
        loaded_session, "example-media"
    )
    gate = next(s for s in run.steps if s["name"] == "integrity_gate")
    assert gate["status"] == "ok"
    assert gate["detail"]["verdict"] == "healthy"


async def test_verify_can_be_skipped(loaded_session: Session) -> None:
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    run = await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(
        loaded_session, "example-media", verify=False
    )
    verify_step = next(s for s in run.steps if s["name"] == "pbs_verify")
    assert verify_step["status"] == "skipped"
    record = loaded_session.exec(select(BackupRecord)).one()
    assert record.verified is False


async def test_failed_verify_fails_the_run(loaded_session: Session) -> None:
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    pbs.verify_result = False

    with pytest.raises(BackupError, match="verify failed"):
        await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(
            loaded_session, "example-media"
        )

    run = loaded_session.exec(select(PipelineRun)).one()
    assert run.status is PipelineStatus.FAILED
    # The record still exists — the backup was taken, its integrity just isn't confirmed.
    record = loaded_session.exec(select(BackupRecord)).one()
    assert record.verified is False


async def test_failed_vzdump_aborts_before_recording(loaded_session: Session) -> None:
    proxmox = DryRunProxmoxAdapter()
    proxmox.next_task_exit_status = "backup failed: got timeout"
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])

    with pytest.raises(BackupError, match="vzdump"):
        await _pipeline(proxmox, pbs).run_for_service(loaded_session, "example-media")

    assert loaded_session.exec(select(BackupRecord)).all() == []
    run = loaded_session.exec(select(PipelineRun)).one()
    assert run.status is PipelineStatus.FAILED


async def test_missing_snapshot_after_dump_is_an_error(loaded_session: Session) -> None:
    """vzdump says OK but PBS shows nothing — usually a storage/datastore mismatch."""
    pbs = DryRunPbsAdapter(simulated_snapshots=[], synthesize_snapshots=False)
    with pytest.raises(BackupError, match="no snapshot"):
        await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(
            loaded_session, "example-media"
        )


async def test_unreachable_pbs_is_marked_indeterminate(loaded_session: Session) -> None:
    """A network blip must be distinguishable from a real failure."""

    class UnreachablePbs(DryRunPbsAdapter):
        async def datastore_status(self, name: str) -> DatastoreStatus:
            raise AdapterUnreachable("pbs down")

    with pytest.raises(AdapterUnreachable):
        await _pipeline(DryRunProxmoxAdapter(), UnreachablePbs()).run_for_service(
            loaded_session, "example-media"
        )

    run = loaded_session.exec(select(PipelineRun)).one()
    assert run.status is PipelineStatus.FAILED
    aborted = next(s for s in run.steps if s["name"] == "aborted")
    assert aborted["status"] == "indeterminate"


async def test_unknown_service_is_rejected(loaded_session: Session) -> None:
    with pytest.raises(BackupError, match="no service registered"):
        await _pipeline(DryRunProxmoxAdapter(), DryRunPbsAdapter()).run_for_service(
            loaded_session, "does-not-exist"
        )


async def test_disabled_service_is_rejected(loaded_session: Session) -> None:
    from orchestrator.db.models import Service

    svc = loaded_session.exec(select(Service).where(Service.slug == "example-media")).one()
    svc.enabled = False
    loaded_session.commit()

    with pytest.raises(BackupError, match="disabled"):
        await _pipeline(DryRunProxmoxAdapter(), DryRunPbsAdapter()).run_for_service(
            loaded_session, "example-media"
        )


async def test_backup_writes_audit_entries(loaded_session: Session) -> None:
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(
        loaded_session, "example-media", actor="tester"
    )
    actions = {e.action for e in loaded_session.exec(select(AuditEntry)).all()}
    assert "backup.start" in actions
    assert "backup.succeeded" in actions


async def test_ct_service_uses_ct_backup_type(loaded_session: Session) -> None:
    """example-home is an LXC container — it must resolve to PBS type 'ct'."""
    ct_snap = Snapshot(
        datastore=get_settings().pbs_datastore,
        backup_type="ct",
        backup_id="200",
        backup_time=1756000000,
    )
    pbs = DryRunPbsAdapter(simulated_snapshots=[ct_snap])
    await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(loaded_session, "example-home")
    record = loaded_session.exec(
        select(BackupRecord).where(BackupRecord.service_slug == "example-home")
    ).one()
    assert record.pbs_snapshot_id.startswith("ct/200/")


async def test_backup_excluded_service_is_refused(loaded_session: Session) -> None:
    """The circular-backup guard: a VM hosting PBS's own storage must never be
    dumped to PBS. Hard refusal, no override."""
    from orchestrator.db.models import Service

    svc = loaded_session.exec(select(Service).where(Service.slug == "example-media")).one()
    svc.backup_excluded = True
    svc.backup_excluded_reason = "hosts the ZFS pool PBS lives on"
    loaded_session.commit()

    with pytest.raises(BackupError, match="backup_excluded"):
        await _pipeline(DryRunProxmoxAdapter(), DryRunPbsAdapter()).run_for_service(
            loaded_session, "example-media"
        )

    # Nothing was dumped and nothing was recorded.
    assert loaded_session.exec(select(BackupRecord)).all() == []


async def test_exclusion_message_carries_the_reason(loaded_session: Session) -> None:
    """The operator needs to know WHY, months later, without reading the code."""
    from orchestrator.db.models import Service

    svc = loaded_session.exec(select(Service).where(Service.slug == "example-media")).one()
    svc.backup_excluded = True
    svc.backup_excluded_reason = "circular: hosts the PBS datastore"
    loaded_session.commit()

    with pytest.raises(BackupError) as exc:
        await _pipeline(DryRunProxmoxAdapter(), DryRunPbsAdapter()).run_for_service(
            loaded_session, "example-media"
        )
    assert "circular: hosts the PBS datastore" in str(exc.value)
    assert "no override" in str(exc.value)


async def test_exclusion_without_a_reason_still_refuses(loaded_session: Session) -> None:
    from orchestrator.db.models import Service

    svc = loaded_session.exec(select(Service).where(Service.slug == "example-media")).one()
    svc.backup_excluded = True
    loaded_session.commit()

    with pytest.raises(BackupError, match="no reason recorded"):
        await _pipeline(DryRunProxmoxAdapter(), DryRunPbsAdapter()).run_for_service(
            loaded_session, "example-media"
        )


# ---------------------------------------------------------------------------
# The integrity gate. This is the whole point of the project: never overwrite a
# good backup with a bad one, and never mistake "don't know" for either answer.
# ---------------------------------------------------------------------------


async def test_gate_refuses_a_failed_service(loaded_session: Session) -> None:
    """FAILED means the service is definitively broken — capturing that state
    over a known-good backup is the exact disaster this prevents."""
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    health = StubHealthEngine(HealthState.FAILED, "mariadb-integrity: table is corrupt")

    with pytest.raises(BackupError, match="FAILED"):
        await _pipeline(DryRunProxmoxAdapter(), pbs, health=health).run_for_service(
            loaded_session, "example-media"
        )

    # No dump was taken and no record was written.
    assert loaded_session.exec(select(BackupRecord)).all() == []
    run = loaded_session.exec(select(PipelineRun)).one()
    assert run.status is PipelineStatus.FAILED
    names = [s["name"] for s in run.steps]
    assert "vzdump_started" not in names  # aborted BEFORE touching Proxmox
    gate = next(s for s in run.steps if s["name"] == "integrity_gate")
    assert gate["status"] == "failed"


async def test_gate_refuses_an_unknown_service_failing_closed(loaded_session: Session) -> None:
    """UNKNOWN must also refuse. A check that could not run is not permission."""
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    health = StubHealthEngine(HealthState.UNKNOWN, "ssh: connection refused")

    with pytest.raises(BackupError, match="UNKNOWN"):
        await _pipeline(DryRunProxmoxAdapter(), pbs, health=health).run_for_service(
            loaded_session, "example-media"
        )

    assert loaded_session.exec(select(BackupRecord)).all() == []
    run = loaded_session.exec(select(PipelineRun)).one()
    gate = next(s for s in run.steps if s["name"] == "integrity_gate")
    # Recorded as indeterminate, NOT failed — the distinction drives whether this
    # service becomes a restore candidate.
    assert gate["status"] == "indeterminate"


async def test_unknown_refusal_says_it_is_not_corruption(loaded_session: Session) -> None:
    """The operator must not read an UNKNOWN abort as 'my data is corrupt'."""
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    health = StubHealthEngine(HealthState.UNKNOWN, "probe timed out")

    with pytest.raises(BackupError) as exc:
        await _pipeline(DryRunProxmoxAdapter(), pbs, health=health).run_for_service(
            loaded_session, "example-media"
        )
    msg = str(exc.value)
    assert "not" in msg and "corruption" in msg
    assert "untouched" in msg  # reassures that the good backup survives


async def test_failed_refusal_flags_a_restore_candidate(loaded_session: Session) -> None:
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    health = StubHealthEngine(HealthState.FAILED, "container exited")

    with pytest.raises(BackupError) as exc:
        await _pipeline(DryRunProxmoxAdapter(), pbs, health=health).run_for_service(
            loaded_session, "example-media"
        )
    assert "restore candidate" in str(exc.value)


async def test_gate_can_be_bypassed_explicitly_and_is_marked(loaded_session: Session) -> None:
    """An override must be possible but never silent."""
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    health = StubHealthEngine(HealthState.FAILED, "would normally refuse")

    run = await _pipeline(DryRunProxmoxAdapter(), pbs, health=health).run_for_service(
        loaded_session, "example-media", gate=False
    )

    assert run.status is PipelineStatus.SUCCEEDED
    gate = next(s for s in run.steps if s["name"] == "integrity_gate")
    assert gate["status"] == "bypassed"
    assert gate["detail"]["ungated"] is True
    # And the engine was never consulted.
    assert health.scanned == []


async def test_gate_runs_before_the_dump_not_after(loaded_session: Session) -> None:
    """Order matters: gating after the dump would already have written bad data."""
    pbs = DryRunPbsAdapter(simulated_snapshots=[_snapshot()])
    run = await _pipeline(DryRunProxmoxAdapter(), pbs).run_for_service(
        loaded_session, "example-media"
    )
    names = [s["name"] for s in run.steps]
    assert names.index("integrity_gate") < names.index("vzdump_started")
