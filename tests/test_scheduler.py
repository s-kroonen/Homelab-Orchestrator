"""Phase 6: retention enforcement, the APScheduler wiring, and how a policy
resolves to services — all without a real PBS or a real clock.

Retention is tested directly against :func:`enforce_retention` with a
recording fake PBS adapter; the scheduler's job set is tested by starting a
real ``AsyncIOScheduler`` inside the test's event loop and inspecting its jobs
(nothing ever fires — the crons are hours away); a firing is simulated by
calling :meth:`BackupScheduler.run_policy` directly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.base import Snapshot
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.config import get_settings
from orchestrator.db.models import (
    AuditEntry,
    BackupPolicy,
    BackupRecord,
    Node,
    PipelineRun,
    Service,
)
from orchestrator.domain.enums import GuestKind, HealthState, PipelineStatus
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.pipelines.backup import BackupPipeline
from orchestrator.scheduler import BackupScheduler, job_id, next_fire_time, resolve_targets
from orchestrator.scheduler.retention import (
    RetentionError,
    enforce_retention,
    validate_retention,
)

DAY = 86400
T0 = 1_756_000_000  # 2025-08-24


class RecordingPbs(DryRunPbsAdapter):
    """Dry-run PBS that remembers every prune/protect call it gets."""

    def __init__(self, snapshots: list[Snapshot], prune_removes: list[Snapshot] | None = None):
        super().__init__(simulated_snapshots=snapshots, synthesize_snapshots=False)
        self.prune_calls: list[dict[str, Any]] = []
        self.protect_calls: list[tuple[int, bool]] = []
        self._prune_removes = prune_removes or []

    async def prune(self, **kwargs: Any) -> list[Snapshot]:  # type: ignore[override]
        self.prune_calls.append(kwargs)
        return list(self._prune_removes)

    async def set_protected(  # type: ignore[override]
        self, datastore: str, *, backup_time: int, protected: bool, **_: Any
    ) -> None:
        self.protect_calls.append((backup_time, protected))


def _snap(when: int, *, verified: bool = False, protected: bool = False) -> Snapshot:
    return Snapshot(
        datastore=get_settings().pbs_datastore,
        backup_type="vm",
        backup_id="100",
        backup_time=when,
        verified=verified,
        protected=protected,
    )


def _policy(session: Session, **kwargs: Any) -> BackupPolicy:
    policy = BackupPolicy(name=kwargs.pop("name", "nightly"), **kwargs)
    session.add(policy)
    session.commit()
    session.refresh(policy)
    return policy


# ---------------------------------------------------------------------------
# validate_retention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"keep_daily": -1},
        {"keep_daily": "7"},
        {"keep_daily": True},
        {"keep_weekly": 4},
        {"protect_latest_verified": "yes"},
        ["keep_daily"],
    ],
)
def test_validate_retention_rejects_bad_shapes(bad: Any) -> None:
    with pytest.raises(ValueError):
        validate_retention(bad)


def test_validate_retention_normalises() -> None:
    assert validate_retention(None) == {}
    assert validate_retention({"keep_daily": 7, "keep_monthly": None}) == {"keep_daily": 7}
    assert validate_retention({"keep_last": 0, "protect_latest_verified": False}) == {
        "keep_last": 0,
        "protect_latest_verified": False,
    }


# ---------------------------------------------------------------------------
# enforce_retention
# ---------------------------------------------------------------------------


async def test_pins_latest_verified_not_latest_overall_then_prunes(
    session: Session, service: Service
) -> None:
    older_verified = _snap(T0, verified=True)
    newest_unverified = _snap(T0 + DAY)
    pbs = RecordingPbs([older_verified, newest_unverified], prune_removes=[_snap(T0 - DAY)])
    policy = _policy(
        session, retention={"keep_daily": 7, "keep_monthly": 6, "protect_latest_verified": True}
    )

    result = await enforce_retention(session, pbs, policy, "thing", settings=get_settings())
    session.commit()

    assert pbs.protect_calls == [(T0, True)]
    assert result.protected == older_verified.snapshot_id
    # Pin happened BEFORE prune — PBS's prune never removes a protected snapshot.
    assert len(pbs.prune_calls) == 1
    call = pbs.prune_calls[0]
    assert call["keep_daily"] == 7 and call["keep_monthly"] == 6
    assert call["backup_type"] == "vm" and call["backup_id"] == "100"
    assert call["dry_run"] is True  # conftest runs with DRY_RUN=true
    assert result.pruned == [_snap(T0 - DAY).snapshot_id]

    audit = session.exec(select(AuditEntry).where(AuditEntry.action == "backup.retention")).one()
    assert audit.actor == "scheduler"
    assert audit.details["protected"] == older_verified.snapshot_id


async def test_db_verified_flag_counts_when_pbs_has_not_caught_up(
    session: Session, service: Service
) -> None:
    """Dry-run PBS never sets ``verified``; the pipeline's BackupRecord does."""
    snap = _snap(T0)
    session.add(BackupRecord(service_slug="thing", pbs_snapshot_id=snap.snapshot_id, verified=True))
    session.commit()
    pbs = RecordingPbs([snap])
    policy = _policy(session, retention={"protect_latest_verified": True})

    result = await enforce_retention(session, pbs, policy, "thing", settings=get_settings())
    session.commit()

    assert pbs.protect_calls == [(T0, True)]
    assert result.protected == snap.snapshot_id
    record = session.exec(select(BackupRecord)).one()
    assert record.protected is True


async def test_pin_moves_forward_and_unpins_only_what_we_pinned(
    session: Session, service: Service
) -> None:
    old = _snap(T0, verified=True, protected=True)
    manual_pin = _snap(T0 - 30 * DAY, verified=True, protected=True)  # operator's, not ours
    new = _snap(T0 + DAY, verified=True)
    session.add(BackupRecord(service_slug="thing", pbs_snapshot_id=old.snapshot_id, protected=True))
    session.add(BackupRecord(service_slug="thing", pbs_snapshot_id=new.snapshot_id))
    session.commit()
    pbs = RecordingPbs([old, manual_pin, new])
    policy = _policy(session, retention={"protect_latest_verified": True, "keep_last": 3})

    result = await enforce_retention(session, pbs, policy, "thing", settings=get_settings())
    session.commit()

    assert (T0 + DAY, True) in pbs.protect_calls
    assert (T0, False) in pbs.protect_calls
    assert all(t != manual_pin.backup_time for t, _ in pbs.protect_calls)
    assert result.unprotected == [old.snapshot_id]
    pinned = {r.pbs_snapshot_id: r.protected for r in session.exec(select(BackupRecord)).all()}
    assert pinned == {old.snapshot_id: False, new.snapshot_id: True}


async def test_already_protected_latest_is_not_re_pinned(
    session: Session, service: Service
) -> None:
    pbs = RecordingPbs([_snap(T0, verified=True, protected=True)])
    policy = _policy(session, retention={"protect_latest_verified": True})
    await enforce_retention(session, pbs, policy, "thing", settings=get_settings())
    assert pbs.protect_calls == []


async def test_no_verified_snapshot_pins_nothing(session: Session, service: Service) -> None:
    pbs = RecordingPbs([_snap(T0), _snap(T0 + DAY)])
    policy = _policy(session, retention={"protect_latest_verified": True, "keep_daily": 2})
    result = await enforce_retention(session, pbs, policy, "thing", settings=get_settings())
    assert pbs.protect_calls == []
    assert result.protected is None
    assert len(pbs.prune_calls) == 1


async def test_protect_disabled_pins_nothing_and_records_no_db_pin(
    session: Session, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PBS_PROTECT_ENABLED", "false")
    config_module.reset_settings_cache()
    snap = _snap(T0, verified=True)
    session.add(BackupRecord(service_slug="thing", pbs_snapshot_id=snap.snapshot_id, verified=True))
    session.commit()
    pbs = RecordingPbs([snap])
    policy = _policy(session, retention={"protect_latest_verified": True, "keep_daily": 3})

    result = await enforce_retention(session, pbs, policy, "thing", settings=get_settings())
    session.commit()

    assert pbs.protect_calls == []
    assert result.protected is None
    assert result.protect_skipped_reason == "PBS_PROTECT_ENABLED=false"
    assert session.exec(select(BackupRecord)).one().protected is False
    assert len(pbs.prune_calls) == 1


async def test_no_keep_counts_skips_prune(session: Session, service: Service) -> None:
    pbs = RecordingPbs([_snap(T0)])
    policy = _policy(session, retention={})
    result = await enforce_retention(session, pbs, policy, "thing", settings=get_settings())
    assert pbs.prune_calls == []
    assert result.prune_skipped_reason


async def test_prune_dry_run_follows_settings(
    session: Session, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    config_module.reset_settings_cache()
    pbs = RecordingPbs([_snap(T0)])
    policy = _policy(session, retention={"keep_daily": 1})
    await enforce_retention(session, pbs, policy, "thing", settings=get_settings())
    assert pbs.prune_calls[0]["dry_run"] is False


async def test_retention_rejects_service_without_guest(session: Session, service: Service) -> None:
    session.add(Service(slug="hostonly", name="Host only", node_id=service.node_id))
    session.commit()
    policy = _policy(session, retention={"keep_daily": 1})
    with pytest.raises(RetentionError):
        await enforce_retention(
            session, RecordingPbs([]), policy, "hostonly", settings=get_settings()
        )


async def test_retention_rejects_bad_retention_shape(session: Session, service: Service) -> None:
    policy = _policy(session, retention={"keep_daily": -3})
    with pytest.raises(ValueError):
        await enforce_retention(session, RecordingPbs([]), policy, "thing", settings=get_settings())


# ---------------------------------------------------------------------------
# targets + scheduler wiring
# ---------------------------------------------------------------------------


def _two_services(session: Session) -> tuple[Service, Service]:
    node = Node(name="eve1", always_on=True)
    session.add(node)
    session.commit()
    session.refresh(node)
    a = Service(slug="alpha", name="A", node_id=node.id, guest_kind=GuestKind.VM, guest_id=101)
    b = Service(slug="beta", name="B", node_id=node.id, guest_kind=GuestKind.CT, guest_id=102)
    session.add(a)
    session.add(b)
    session.commit()
    return a, b


def test_resolve_targets_unions_explicit_and_attached(session: Session) -> None:
    a, b = _two_services(session)
    policy = _policy(session, targets={"services": ["beta"]})
    a.backup_policy_id = policy.id
    b.backup_policy_id = policy.id  # listed both ways: still only once
    session.add(a)
    session.add(b)
    session.commit()
    assert resolve_targets(session, policy) == ["beta", "alpha"]


def test_next_fire_time_and_invalid_cron() -> None:
    assert next_fire_time("") is None
    assert next_fire_time("0 3 * * *") is not None
    with pytest.raises(ValueError):
        next_fire_time("not a cron")


def _bundle(pbs: DryRunPbsAdapter | None = None) -> AdapterBundle:
    return AdapterBundle(
        power=DryRunPowerAdapter(),
        proxmox=DryRunProxmoxAdapter(),
        pbs=pbs or DryRunPbsAdapter(),
        proxy=DryRunProxyAdapter(),
        notifier=NullNotifier(),
    )


async def test_sync_jobs_registers_valid_crons_and_reports_bad_ones(session: Session) -> None:
    _policy(session, name="nightly", schedule_cron="0 3 * * *")
    _policy(session, name="manual", schedule_cron="")
    _policy(session, name="broken", schedule_cron="61 25 * * *")

    sched = BackupScheduler(adapters=_bundle(), settings=get_settings())
    sched.start()
    try:
        assert sched.running
        assert sched.next_run_time("nightly") is not None
        assert sched.next_run_time("manual") is None
        assert sched.next_run_time("broken") is None
        assert "broken" in sched.errors

        # An edit + resync replaces/removes jobs without a restart.
        nightly = session.exec(select(BackupPolicy).where(BackupPolicy.name == "nightly")).one()
        nightly.schedule_cron = ""
        session.add(nightly)
        session.commit()
        sched.sync_jobs()
        assert sched.next_run_time("nightly") is None
        assert sched._scheduler.get_job(job_id("nightly")) is None
    finally:
        await sched.shutdown()
    assert not sched.running


class StubHealthEngine:
    def __init__(self, state: HealthState = HealthState.HEALTHY) -> None:
        self.state = state

    def can_verify(self, session: Session, service: Any) -> bool:
        return True

    async def scan(self, session: Session, service: Service) -> ServiceVerdict:
        return ServiceVerdict(state=self.state, reason="stub", probe_results=[])


def _scheduler_with(pbs: DryRunPbsAdapter, state: HealthState) -> BackupScheduler:
    adapters = _bundle(pbs)
    settings = get_settings()
    return BackupScheduler(
        adapters=adapters,
        settings=settings,
        pipeline_factory=lambda: BackupPipeline(
            proxmox=adapters.proxmox,
            pbs=adapters.pbs,
            settings=settings,
            health=StubHealthEngine(state),  # type: ignore[arg-type]
        ),
    )


async def test_run_policy_backs_up_each_target_then_enforces_retention(session: Session) -> None:
    _two_services(session)
    _policy(
        session,
        targets={"services": ["alpha", "beta"]},
        retention={"keep_daily": 7, "protect_latest_verified": True},
    )
    pbs = RecordingPbs([])
    pbs.synthesize_snapshots = True  # let the dry-run backup locate a snapshot
    sched = _scheduler_with(pbs, HealthState.HEALTHY)

    outcomes = await sched.run_policy("nightly")

    assert set(outcomes) == {"alpha", "beta"}
    assert all(o["backup"] == "succeeded" for o in outcomes.values())
    assert all("error" not in o["retention"] for o in outcomes.values())
    assert len(pbs.prune_calls) == 2
    session.expire_all()
    runs = session.exec(select(PipelineRun)).all()
    assert [r.status for r in runs] == [PipelineStatus.SUCCEEDED] * 2
    actors = {
        e.actor for e in session.exec(select(AuditEntry)).all() if e.action.startswith("backup.")
    }
    assert actors == {"scheduler"}


async def test_failed_backup_never_prunes(session: Session) -> None:
    _two_services(session)
    _policy(session, targets={"services": ["alpha"]}, retention={"keep_daily": 1})
    pbs = RecordingPbs([])
    sched = _scheduler_with(pbs, HealthState.FAILED)

    outcomes = await sched.run_policy("nightly")

    assert outcomes["alpha"]["backup"] == "skipped"
    assert "retention" not in outcomes["alpha"]
    assert pbs.prune_calls == []
    assert pbs.protect_calls == []
    session.expire_all()
    skipped = session.exec(select(AuditEntry).where(AuditEntry.action == "backup.skipped")).one()
    assert skipped.actor == "scheduler" and skipped.details["why"] == "unhealthy"


async def test_run_policy_for_vanished_policy_is_a_no_op(session: Session) -> None:
    sched = _scheduler_with(RecordingPbs([]), HealthState.HEALTHY)
    assert await sched.run_policy("does-not-exist") == {}


async def test_a_due_job_actually_fires_through_apscheduler(session: Session) -> None:
    """Not just run_policy called by hand: the registered cron job, made due
    now, runs on the event loop via APScheduler's asyncio executor."""
    _two_services(session)
    _policy(session, schedule_cron="0 3 * * *", targets={"services": ["alpha"]})
    pbs = RecordingPbs([])
    pbs.synthesize_snapshots = True
    sched = _scheduler_with(pbs, HealthState.HEALTHY)
    sched.start()
    try:
        sched._scheduler.modify_job(job_id("nightly"), next_run_time=datetime.now(UTC))
        sched._scheduler.wakeup()
        run = None
        for _ in range(200):
            session.expire_all()
            run = session.exec(select(PipelineRun)).first()
            if run is not None and run.status is not PipelineStatus.RUNNING:
                break
            await asyncio.sleep(0.02)
        assert run is not None
        assert run.status is PipelineStatus.SUCCEEDED
        assert len(pbs.prune_calls) == 0  # policy sets no keep_* counts
        # ...and it was rescheduled for the next 03:00, not dropped.
        assert sched.next_run_time("nightly") is not None
    finally:
        await sched.shutdown()
