"""OR-98: one backup per service at a time, backups by hand in the queue, deadlines learned
per guest, and a verify that outlasts its deadline followed instead of failed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.errors import TaskTimeout
from orchestrator.adapters.pbs.base import PbsTask, Snapshot
from orchestrator.config import get_settings
from orchestrator.db.models import BackupRecord, PipelineRun, Service
from orchestrator.domain.enums import PipelineKind, PipelineStatus
from orchestrator.pipelines import backup_timing
from orchestrator.pipelines.backup import (
    BackupAlreadyRunning,
    follow_verify,
    sync_verified,
)
from tests import test_backup_wake as base
from tests.test_backup_retry_restore import announced as announced  # the fixture
from tests.test_backup_wake import World, WorldPbs, _world
from tests.test_cleanup import _pipe, _sched, _until

topo = base.topo
_fast_wake = base._fast_wake

UPID = "UPID:pbs:0001:0001:0003:6B07E718:verify_snapshot:main\\x3a-vm-101:token:"


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "backup_power_off_grace_s", 0)
    monkeypatch.setattr(get_settings(), "cleanup_poll_s", 0.001)


class SlowPbs(WorldPbs):
    """A PBS whose verify outlasts the deadline, then finishes after a few polls."""

    def __init__(self, world: World, *, polls: int = 2, exit_status: str = "OK") -> None:
        super().__init__(world)
        self.polls_left = polls
        self.exit_status = exit_status
        self.timeouts: list[int] = []
        self.gate: asyncio.Event | None = None
        #: Hold only this guest's verify at the gate (None: every one).
        self.gate_only: str | None = None
        self.slow = True

    async def verify_snapshot(self, datastore: str, *, timeout_s: int = 1800, **kw: Any) -> bool:
        self.timeouts.append(timeout_s)
        self.w.events.append("verify")
        if self.gate is not None and self.gate_only in (None, kw.get("backup_id")):
            await self.gate.wait()
        if self.slow:
            raise TaskTimeout(f"PBS task {UPID} still running after {timeout_s}s", upid=UPID)
        return True

    async def task(self, upid: str, *, log_lines: int = 50) -> PbsTask:
        if self.polls_left > 0:
            self.polls_left -= 1
            return PbsTask(upid=upid, running=True)
        self.w.events.append("verify_done")
        return PbsTask(
            upid=upid, running=False, exit_status=self.exit_status, log_tail=["TASK ERROR: bad"]
        )


def _run_with(session: Session, service: Service, steps: list[dict[str, Any]]) -> PipelineRun:
    run = PipelineRun(
        kind=PipelineKind.BACKUP,
        correlation_id="t",
        service_id=service.id,
        status=PipelineStatus.SUCCEEDED,
        steps=steps,
    )
    session.add(run)
    session.commit()
    return run


def _step(name: str, seconds: int, status: str = "ok") -> dict[str, Any]:
    start = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
    return {
        "name": name,
        "status": status,
        "started_at": start.isoformat(),
        "finished_at": (start + timedelta(seconds=seconds)).isoformat(),
        "detail": {},
    }


# -- deadlines learned per guest -----------------------------------------------------------


def test_with_no_history_the_default_is_the_deadline(
    session: Session, topo: dict[str, Any]
) -> None:
    s = get_settings()
    d = backup_timing.deadline(session, s, topo["app"].id, "verify")
    assert d.seconds == s.verify_task_timeout_s
    assert d.why.startswith("no history yet")
    assert backup_timing.deadline(session, s, topo["app"].id, "dump").seconds == (
        s.backup_task_timeout_s
    )


def test_with_no_history_a_big_snapshot_raises_the_verify_deadline(
    session: Session, topo: dict[str, Any]
) -> None:
    s = get_settings()
    size = 128 * 1024**3  # 128 GiB at 15 MiB/s: about 2.4 h
    d = backup_timing.deadline(session, s, topo["app"].id, "verify", size_bytes=size)
    assert d.seconds == int(size / (s.verify_min_mib_s * 1024 * 1024))
    assert "128 GiB" in d.why


def test_history_scales_the_deadline_down_and_up(session: Session, topo: dict[str, Any]) -> None:
    s = get_settings()
    app, db = topo["app"], topo["db"]
    # app verifies in about 10 minutes: 2 x 11 min + 5 min, well under the default 30.
    for seconds in (540, 660, 600):
        _run_with(session, app, [_step("vzdump_finished", 300), _step("pbs_verify", seconds)])
    d = backup_timing.deadline(session, s, app.id, "verify")
    assert d.seconds == 660 * 2 + 300
    assert d.history_s == (600, 660, 540)  # newest first
    # db took 50 minutes once: the deadline goes past the default.
    _run_with(session, db, [_step("pbs_verify", 3000)])
    assert backup_timing.deadline(session, s, db.id, "verify").seconds == 3000 * 2 + 300
    # a failed verify does not count
    _run_with(session, db, [_step("pbs_verify", 9000, status="failed")])
    assert backup_timing.deadline(session, s, db.id, "verify").seconds == 3000 * 2 + 300


def test_the_deadline_stays_between_the_floor_and_the_ceiling(
    session: Session, topo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    s = get_settings()
    app, db = topo["app"], topo["db"]
    _run_with(session, app, [_step("pbs_verify", 5)])
    d = backup_timing.deadline(session, s, app.id, "verify")
    assert d.seconds == s.backup_deadline_min_s and "minimum" in d.why
    monkeypatch.setattr(s, "verify_task_max_s", 3600)
    _run_with(session, db, [_step("pbs_verify", 7200)])
    d = backup_timing.deadline(session, s, db.id, "verify")
    assert d.seconds == 3600 and "capped" in d.why


# -- a verify that outlasts its deadline ---------------------------------------------------


async def test_a_verify_past_its_deadline_is_not_a_failed_backup(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo, up={"storage", "compute", "gw"}, running={100, 101, 102, 103})
    pbs = SlowPbs(world)

    run = await _pipe(world, pbs).run_for_service(session, "gwsvc", actor="t")

    assert run.status is PipelineStatus.SUCCEEDED, run.error
    step = next(s for s in run.steps if s["name"] == "pbs_verify")
    assert step["status"] == "running"
    assert step["detail"]["upid"] == UPID
    assert step["detail"]["deadline_s"] == pbs.timeouts[0]
    rec = session.exec(select(BackupRecord).where(BackupRecord.pipeline_run_id == run.id)).one()
    assert rec.verify_state == "running" and rec.verify_upid == UPID and not rec.verified
    # the dump's deadline is in its step too
    started = next(s for s in run.steps if s["name"] == "vzdump_started")
    assert started["detail"]["deadline_s"] == get_settings().backup_task_timeout_s

    # Followed to its end: the record and the run step say what PBS said.
    assert await follow_verify(session, pbs, get_settings(), rec.id, poll_s=0.001) == "ok"
    session.expire_all()
    rec = session.get(BackupRecord, rec.id)
    assert rec.verified and rec.verify_state == "ok" and rec.verified_at is not None
    run = session.get(PipelineRun, run.id)
    step = next(s for s in run.steps if s["name"] == "pbs_verify")
    assert step["status"] == "ok" and step["detail"]["followed"] is True
    assert run.status is PipelineStatus.SUCCEEDED


async def test_a_followed_verify_that_fails_fails_the_run_and_says_so(
    session: Session, topo: dict[str, Any], announced: Any
) -> None:
    world = _world(topo, up={"storage", "compute", "gw"}, running={100, 101, 102, 103})
    pbs = SlowPbs(world, exit_status="verification failed")
    run = await _pipe(world, pbs).run_for_service(session, "gwsvc", actor="t")
    rec = session.exec(select(BackupRecord).where(BackupRecord.pipeline_run_id == run.id)).one()

    assert await follow_verify(session, pbs, get_settings(), rec.id, poll_s=0.001) == "failed"

    session.expire_all()
    run = session.get(PipelineRun, run.id)
    assert run.status is PipelineStatus.FAILED and "verify failed" in (run.error or "")
    assert session.get(BackupRecord, rec.id).verify_state == "failed"
    assert "backup_failed" in announced.kinds()


async def test_a_verify_is_followed_no_longer_than_its_ceiling(
    session: Session, topo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(topo, up={"storage", "compute", "gw"}, running={100, 101, 102, 103})
    pbs = SlowPbs(world, polls=10**6)
    run = await _pipe(world, pbs).run_for_service(session, "gwsvc", actor="t")
    rec = session.exec(select(BackupRecord).where(BackupRecord.pipeline_run_id == run.id)).one()
    monkeypatch.setattr(get_settings(), "verify_task_max_s", 0)

    assert await follow_verify(session, pbs, get_settings(), rec.id, poll_s=0.001) == "unknown"
    session.expire_all()
    assert session.get(BackupRecord, rec.id).verify_state == "unknown"


# -- one backup per service at a time ------------------------------------------------------


async def test_a_second_backup_of_the_same_service_is_refused_while_one_runs(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo, up={"storage", "compute", "gw"}, running={100, 101, 102, 103})
    pbs = SlowPbs(world)
    pbs.slow = False
    pbs.gate = asyncio.Event()
    first = asyncio.create_task(_pipe(world, pbs).run_for_service(session, "gwsvc", actor="t"))
    await _until(lambda: "verify" in world.events)

    with pytest.raises(BackupAlreadyRunning) as err:
        await _pipe(world, pbs).run_for_service(session, "gwsvc", actor="t")
    assert err.value.run_id is not None

    pbs.gate.set()
    run = await first
    assert run.status is PipelineStatus.SUCCEEDED
    runs = session.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.BACKUP)).all()
    assert len(runs) == 1  # the refused one recorded nothing
    # and once it is done, the next one may run
    again = await _pipe(world, pbs).run_for_service(session, "gwsvc", actor="t")
    assert again.status is PipelineStatus.SUCCEEDED


# -- by hand, in the queue -----------------------------------------------------------------


async def test_a_backup_by_hand_queues_and_its_pending_verify_keeps_the_storage_up(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo, up={"compute", "gw"}, running={101, 102, 103})
    pbs = SlowPbs(world, polls=3)
    sched = _sched(world, pbs)

    job = sched.queue_backup("gwsvc", actor="admin")
    assert sched.backup_busy("gwsvc") is not None
    await _until(lambda: job.run_id is not None)
    # the verify outlasts its deadline: a verify job follows it, and the storage stays held
    await _until(lambda: any(j["kind"] == "verify" for j in sched.queue()["jobs"]))
    assert "power_off:storage" not in world.events
    await _until(lambda: not sched.queue()["jobs"])
    await _until(lambda: "power_off:storage" in world.events)

    # PBS finished first, then the storage was put back
    assert world.index("verify_done") < world.index("power_off:storage")
    assert world.count("power_on:storage") == 1
    session.expire_all()
    rec = session.exec(select(BackupRecord)).one()
    assert rec.verified and rec.verify_state == "ok"
    run = session.get(PipelineRun, job.run_id)
    assert run.status is PipelineStatus.SUCCEEDED
    assert sched.backup_busy("gwsvc") is None


async def test_a_queued_backup_waits_for_the_job_ahead_and_shares_its_wake(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo, up={"compute", "gw"}, running={101, 102, 103})
    pbs = SlowPbs(world)
    pbs.slow = False
    pbs.gate = asyncio.Event()
    sched = _sched(world, pbs)

    first = sched.queue_backup("app", actor="admin")
    await _until(lambda: "verify" in world.events)
    second = sched.queue_backup("db", actor="admin")
    jobs = sched.queue()["jobs"]
    assert [(j["service"], j["state"]) for j in jobs] == [("app", "running"), ("db", "waiting")]
    assert second.run_id is None

    pbs.gate.set()
    await _until(lambda: not sched.queue()["jobs"])
    await _until(lambda: "power_off:storage" in world.events)
    assert first.run_id is not None and second.run_id is not None
    assert world.count("power_on:storage") == 1  # one wake for both


async def test_a_policy_batch_skips_a_service_backed_up_by_hand_right_now(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo, up={"storage", "compute", "gw"}, running={100, 101, 102, 103})
    pbs = SlowPbs(world)
    pbs.slow = False
    pbs.gate = asyncio.Event()
    pbs.gate_only = "103"  # only gwsvc's verify waits
    by_hand = asyncio.create_task(_pipe(world, pbs).run_for_service(session, "gwsvc", actor="t"))
    await _until(lambda: "verify" in world.events)

    outcomes = await _sched(world, pbs).run_policy("nightly")

    assert outcomes["app"]["backup"] == "succeeded"
    assert outcomes["gwsvc"]["backup"] == "skipped"
    assert outcomes["gwsvc"]["why"] == "already_running"
    assert outcomes["gwsvc"]["retry"] is False
    pbs.gate.set()
    assert (await by_hand).status is PipelineStatus.SUCCEEDED


# -- PBS's verification brought into the records -------------------------------------------


def test_records_pbs_verified_are_marked_verified(session: Session, topo: dict[str, Any]) -> None:
    when = datetime(2026, 11, 26, 8, 0, 4, tzinfo=UTC)
    snap = Snapshot(
        datastore="main",
        backup_type="vm",
        backup_id="101",
        backup_time=int(when.timestamp()),
        verified=True,
    )
    other = Snapshot(datastore="main", backup_type="vm", backup_id="101", backup_time=1)
    for s in (snap, other):
        session.add(BackupRecord(service_slug="app", pbs_snapshot_id=s.snapshot_id))
    session.commit()

    assert sync_verified(session, [snap, other]) == 1
    recs = {r.pbs_snapshot_id: r for r in session.exec(select(BackupRecord)).all()}
    assert recs[snap.snapshot_id].verified and recs[snap.snapshot_id].verify_state == "ok"
    assert not recs[other.snapshot_id].verified
    assert sync_verified(session, [snap]) == 0  # nothing left to change


# -- the endpoint -------------------------------------------------------------------------


async def test_the_endpoint_refuses_a_second_backup_with_its_run(
    session: Session, topo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from orchestrator import scheduler
    from orchestrator.web.routers.backup import start_backup

    class Busy:
        running = True

        def backup_busy(self, slug: str) -> dict[str, Any]:
            return {"state": "running", "run_id": 41, "job": None}

    monkeypatch.setattr(scheduler, "current", lambda: Busy())
    world = _world(topo)
    adapters = _sched(world, SlowPbs(world))._adapters
    with pytest.raises(HTTPException) as err:
        await start_backup(session, adapters, get_settings(), "app", actor="t")
    assert err.value.status_code == 409
    assert err.value.detail["run_id"] == 41
    assert "already running (run #41)" in err.value.detail["message"]
    # a target that cannot be backed up at all is refused before anything is queued
    with pytest.raises(HTTPException) as err:
        await start_backup(session, adapters, get_settings(), "truenas", actor="t")
    assert err.value.status_code == 400
