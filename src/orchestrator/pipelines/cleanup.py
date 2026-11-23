"""Clean a PBS datastore while it is awake: prune by the backup policies, then let PBS's
garbage collection give the space back, waiting until it is done (docs/cleanup.md).

The datastore here lives on storage that is only up while the orchestrator wakes it, so
PBS's own prune and GC jobs (fixed times) find it offline. A cleanup runs inside the
scheduler's queue instead: the queue wakes and holds the storage (``prepared``), runs the
cleanup and the backups around it one after another, and puts the node back after the last.
This module never wakes or powers anything itself.

Steps, in order, each on the run:

1. ``storage`` - the queue's wake (what was woken, what is held), or why it failed.
2. ``space_before`` - the datastore's size, used and free.
3. ``prune`` - each service by its backup policy's keep rules (pin the newest verified
   first, as after a backup); groups no policy keeps are listed, and pruned only when the
   policy says so. One service failing does not stop the others.
4. ``gc`` - PBS's garbage collection when it is due, followed to its end: phase and
   percent while it runs, what it removed and left pending after. Bounded by
   ``max_wait_h``: past it the GC is stopped in PBS (it starts over next time).
5. ``space_after`` - size, used and free again, with what GC removed and left pending.
6. ``sync_records`` - backup records whose snapshot PBS no longer has are dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.pbs.base import DatastoreStatus, PbsAdapter, PbsTask
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import BackupPolicy, BackupRecord, CleanupPolicy, PipelineRun, Service
from orchestrator.domain.enums import (
    AuditResult,
    CleanupGcMode,
    GuestKind,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.backup import task_error_lines
from orchestrator.pipelines.backup_wake import WakeContext
from orchestrator.pipelines.interrupted import fail_unexpected
from orchestrator.pipelines.steps import StepRecorder

log = get_logger(__name__)

_KIND_TO_PBS_TYPE: dict[GuestKind, str] = {GuestKind.VM: "vm", GuestKind.CT: "ct"}
_PHASE_RE = re.compile(r"Start GC phase\s*(\d)", re.I)
_MARKED_RE = re.compile(r"marked (\d+)% \((\d+) of (\d+) index files\)", re.I)
_PROCESSED_RE = re.compile(r"processed (\d+)% \((\d+) chunks\)", re.I)
_REMOVED_RE = re.compile(r"Removed garbage:\s*(.+)$", re.I)
_PENDING_RE = re.compile(r"Pending removals:\s*(.+?)(?:\s*\(|$)", re.I)


class CleanupError(Exception):
    """The cleanup could not run at all (the storage was not woken, PBS refused)."""


def gc_progress(log_tail: list[str]) -> dict[str, Any]:
    """Where a running GC is, from the end of its log: ``phase`` (1 marks the chunks in
    use, 2 removes the rest), ``percent`` and, in phase 1, the index files read."""
    out: dict[str, Any] = {}
    for line in log_tail:
        if m := _PHASE_RE.search(line):
            out = {"phase": int(m.group(1)), "percent": 0}
        elif m := _MARKED_RE.search(line):
            out.update(
                phase=1, percent=int(m.group(1)), indexes=int(m.group(2)), of=int(m.group(3))
            )
        elif m := _PROCESSED_RE.search(line):
            out.update(phase=2, percent=int(m.group(1)), chunks=int(m.group(2)))
    return out


def gc_summary(log_tail: list[str]) -> dict[str, str]:
    """What a finished GC says it removed and left pending, as PBS wrote it."""
    out: dict[str, str] = {}
    for line in log_tail:
        if m := _REMOVED_RE.search(line):
            out["removed"] = m.group(1).strip()
        elif m := _PENDING_RE.search(line):
            out["pending"] = m.group(1).strip()
    return out


def _space(status: DatastoreStatus) -> dict[str, Any]:
    return {
        "datastore": status.name,
        "total_bytes": status.total_bytes,
        "used_bytes": status.used_bytes,
        "available_bytes": status.available_bytes,
        "used_pct": round(status.used_fraction * 100, 1),
    }


class CleanupPipeline:
    def __init__(self, *, pbs: PbsAdapter, settings: Settings, poll_s: float | None = None) -> None:
        self._pbs = pbs
        self._settings = settings
        self._poll_s = settings.cleanup_poll_s if poll_s is None else poll_s

    def datastore(self, policy: CleanupPolicy) -> str:
        return policy.datastore or self._settings.pbs_datastore

    async def run(
        self,
        session: Session,
        policy: CleanupPolicy,
        *,
        prepared: WakeContext | None,
        actor: str = "scheduler",
        reason: str = "",
        stop: asyncio.Event | None = None,
        only_policy: str | None = None,
    ) -> PipelineRun:
        """Run one cleanup. Returns the finished run (SUCCEEDED or FAILED); never raises
        for a failed cleanup, only for an error it has no branch for.

        ``only_policy``: prune just that backup policy's services ("Prune now" on a backup
        policy); groups no policy keeps are then neither listed nor touched."""
        datastore = self.datastore(policy)
        run = PipelineRun(
            kind=PipelineKind.CLEANUP,
            correlation_id=new_correlation_id(),
            status=PipelineStatus.RUNNING,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        steps = StepRecorder(session, run)
        target = f"cleanup:{policy.name}"
        audit.record(
            session,
            actor=actor,
            action="cleanup.start",
            target=target,
            correlation_id=run.correlation_id,
            details={"run_id": run.id, "datastore": datastore, "reason": reason},
        )
        session.commit()
        steps.record(
            "cleanup",
            "ok",
            detail={"policy": policy.name, "datastore": datastore, "reason": reason},
        )
        problems: list[str] = []
        try:
            self._storage(steps, prepared)
            before = await self._space(steps, "space_before", datastore)
            if policy.prune:
                problems += await self._prune(session, steps, policy, datastore, actor, only_policy)
            else:
                steps.record("prune", "skipped", detail={"reason": "the policy does not prune"})
            gc_problem = await self._gc(session, steps, policy, datastore, before, stop)
            if gc_problem:
                problems.append(gc_problem)
            await self._space(steps, "space_after", datastore, gc=True)
            await self._sync_records(session, steps, datastore)
        except (CleanupError, AdapterError) as exc:
            steps.record(
                "aborted", "failed", detail={"error": str(exc), "type": type(exc).__name__}
            )
            problems.append(str(exc))
        except BaseException as exc:
            fail_unexpected(session, run, exc, actor=actor, action="cleanup.failed")
            raise

        run.status = PipelineStatus.FAILED if problems else PipelineStatus.SUCCEEDED
        run.finished_at = datetime.now(UTC)
        run.error = "; ".join(problems)[:2000] if problems else None
        session.add(run)
        audit.record(
            session,
            actor=actor,
            action="cleanup.failed" if problems else "cleanup.succeeded",
            target=target,
            result=AuditResult.ERROR if problems else AuditResult.OK,
            correlation_id=run.correlation_id,
            details={"run_id": run.id, "datastore": datastore, "problems": problems},
        )
        session.commit()
        if problems:
            log.error(
                "cleanup.failed",
                policy=policy.name,
                problems=problems,
                impact="Old snapshots or unused chunks stay on the datastore until it works.",
            )
            try:
                await announce(
                    session,
                    level="job",
                    key=target,
                    name=f"Cleanup {policy.name}",
                    event="cleanup_failed",
                    severity="warn",
                    title=f"Cleanup {policy.name} of datastore {datastore} failed",
                    body="\n".join(problems)[:1500],
                    scope={"policy": policy.name},
                )
            except Exception:  # announce is best-effort
                log.warning("cleanup.announce_failed", policy=policy.name)
        return run

    # -- steps ------------------------------------------------------------------------------

    @staticmethod
    def _storage(steps: StepRecorder, prepared: WakeContext | None) -> None:
        if prepared is None:
            steps.record("storage", "skipped", detail={"reason": "nothing to wake"})
            return
        if prepared.error:
            steps.record("storage", "failed", detail={"error": prepared.error})
            raise CleanupError(f"the storage could not be woken: {prepared.error}")
        if not prepared.results and not prepared.held:
            steps.record("storage", "ok", detail={"reason": "nothing to wake"})
            return
        steps.record(
            "storage",
            "ok",
            detail={
                "woken": {slug: detail for slug, (_s, detail) in prepared.results.items()},
                "held": [h.node_name for h in prepared.held],
            },
        )

    async def _space(
        self, steps: StepRecorder, name: str, datastore: str, *, gc: bool = False
    ) -> DatastoreStatus:
        started = datetime.now(UTC)
        status = await self._pbs.datastore_status(datastore)
        detail = _space(status)
        if gc:
            try:
                gcs = await self._pbs.garbage_collection_status(datastore)
                detail.update(
                    gc_removed_bytes=gcs.removed_bytes, gc_pending_bytes=gcs.pending_bytes
                )
            except (AdapterError, NotImplementedError):
                pass
        steps.record(name, "ok", started_at=started, detail=detail)
        return status

    async def _prune(
        self,
        session: Session,
        steps: StepRecorder,
        policy: CleanupPolicy,
        datastore: str,
        actor: str,
        only_policy: str | None = None,
    ) -> list[str]:
        """Prune by each backup policy, then list (or prune) the groups no policy keeps."""
        from orchestrator.scheduler.retention import enforce_retention

        started = datetime.now(UTC)
        removed: dict[str, int] = {}
        errors: dict[str, str] = {}
        owned: set[str] = set()
        if datastore != self._settings.pbs_datastore:
            steps.record(
                "prune",
                "skipped",
                detail={
                    "reason": f"backups are kept on {self._settings.pbs_datastore!r}; "
                    f"no backup policy keeps snapshots on {datastore!r}"
                },
            )
            return []
        services = session.exec(
            select(Service).where(col(Service.backup_policy_id).is_not(None))
        ).all()
        policies = {p.id: p for p in session.exec(select(BackupPolicy)).all()}
        for svc in sorted(services, key=lambda s: s.slug):
            pbs_type = _KIND_TO_PBS_TYPE.get(svc.guest_kind)
            if pbs_type is None or svc.guest_id is None:
                continue
            owned.add(f"{pbs_type}/{svc.guest_id}")
            bpol = policies.get(svc.backup_policy_id)
            if bpol is None or (only_policy and bpol.name != only_policy):
                continue
            try:
                result = await enforce_retention(
                    session, self._pbs, bpol, svc.slug, settings=self._settings, actor=actor
                )
                session.commit()
                removed[svc.slug] = len(result.pruned)
            except Exception as exc:  # one service must not stop the others
                session.rollback()
                errors[svc.slug] = str(exc)[:300]

        unowned = [] if only_policy else await self._unowned(datastore, owned)
        unowned_removed = 0
        keep = policy.prune_unowned_keep_last
        if keep and unowned:
            for group in unowned:
                backup_type, backup_id = group.split("/", 1)
                try:
                    gone = await self._pbs.prune(
                        datastore=datastore,
                        backup_type=backup_type,
                        backup_id=backup_id,
                        keep_last=keep,
                        dry_run=self._settings.dry_run,
                    )
                    unowned_removed += len(gone)
                except AdapterError as exc:
                    errors[group] = str(exc)[:300]
        detail: dict[str, Any] = {
            "removed": sum(removed.values()) + unowned_removed,
            "by_service": removed,
            "unowned": unowned,
            "unowned_action": f"kept the last {keep}" if keep else "left alone",
        }
        if errors:
            detail["errors"] = errors
        steps.record("prune", "failed" if errors else "ok", started_at=started, detail=detail)
        return [f"prune {name}: {err}" for name, err in errors.items()]

    async def _unowned(self, datastore: str, owned: set[str]) -> list[str]:
        """Backup groups on the datastore that no service's backup policy keeps."""
        try:
            snapshots = await self._pbs.list_snapshots(datastore)
        except AdapterError:
            return []
        return sorted({s.group for s in snapshots} - owned)

    def _gc_due(
        self, session: Session, policy: CleanupPolicy, before: DatastoreStatus
    ) -> str | None:
        """None when a GC should run now, else why not."""
        if policy.gc_mode is CleanupGcMode.OFF:
            return "the policy does not run garbage collection"
        if policy.gc_mode is CleanupGcMode.ABOVE:
            pct = before.used_fraction * 100
            if before.total_bytes and pct < policy.gc_above_pct:
                return f"{pct:.0f}% full, under the policy's {policy.gc_above_pct}%"
            return None
        if policy.gc_interval_h <= 0:
            return None
        last = last_gc_at(session, policy.datastore or self._settings.pbs_datastore)
        if last is not None and datetime.now(UTC) - last < timedelta(hours=policy.gc_interval_h):
            hours = (datetime.now(UTC) - last).total_seconds() / 3600
            return (
                f"the last one finished {hours:.1f} h ago (at most every {policy.gc_interval_h} h)"
            )
        return None

    async def _gc(
        self,
        session: Session,
        steps: StepRecorder,
        policy: CleanupPolicy,
        datastore: str,
        before: DatastoreStatus,
        stop: asyncio.Event | None,
    ) -> str | None:
        """Run PBS's GC and wait for it. Returns a problem, or None."""
        not_due = self._gc_due(session, policy, before)
        if not_due:
            steps.record("gc", "skipped", detail={"reason": not_due})
            return None
        started = datetime.now(UTC)
        upid = await self._pbs.start_garbage_collection(datastore)
        deadline = time.monotonic() + policy.max_wait_h * 3600
        steps.record("gc", "running", started_at=started, detail={"upid": upid})
        task: PbsTask | None = None
        while True:
            if stop is not None and stop.is_set():
                return await self._stop_gc(steps, upid, started, "stopped by request")
            try:
                task = await self._pbs.task(upid)
            except AdapterError as exc:
                # PBS blipped; keep waiting until the deadline rather than give up on a GC
                # that is very likely still running.
                log.warning("cleanup.gc.poll_failed", upid=upid, error=str(exc))
                task = None
            if task is not None and not task.running:
                break
            if task is not None:
                steps.record(
                    "gc",
                    "running",
                    started_at=started,
                    detail={"upid": upid, **gc_progress(task.log_tail)},
                )
            if time.monotonic() >= deadline:
                return await self._stop_gc(
                    steps,
                    upid,
                    started,
                    f"still running after {policy.max_wait_h} h (the policy's wait)",
                )
            await _pause(self._poll_s, stop)
        assert task is not None
        summary = gc_summary(task.log_tail)
        if not task.succeeded:
            why = task_error_lines(task.log_tail) or task.exit_status or "unknown"
            steps.record(
                "gc",
                "failed",
                started_at=started,
                detail={"upid": upid, "exit_status": task.exit_status, "log": why},
            )
            return f"garbage collection failed: {why}"
        steps.record(
            "gc", "ok", started_at=started, detail={"upid": upid, "exit_status": "OK", **summary}
        )
        return None

    async def _stop_gc(self, steps: StepRecorder, upid: str, started: datetime, why: str) -> str:
        try:
            await self._pbs.stop_task(upid)
        except AdapterError as exc:
            log.warning("cleanup.gc.stop_failed", upid=upid, error=str(exc))
        steps.record(
            "gc",
            "failed",
            started_at=started,
            detail={"upid": upid, "error": f"{why}: stopped in PBS; it starts over next time"},
        )
        return f"garbage collection {why}"

    async def _sync_records(self, session: Session, steps: StepRecorder, datastore: str) -> None:
        """Drop backup records whose snapshot PBS no longer has (pruned here, or by hand)."""
        if datastore != self._settings.pbs_datastore:
            steps.record("sync_records", "skipped", detail={"reason": "no records kept for it"})
            return
        try:
            snapshots = await self._pbs.list_snapshots(datastore)
        except AdapterError as exc:
            steps.record("sync_records", "failed", detail={"error": str(exc)})
            return
        present = {s.snapshot_id for s in snapshots}
        records = session.exec(select(BackupRecord)).all()
        gone = [r for r in records if r.pbs_snapshot_id not in present]
        if self._settings.dry_run:
            steps.record("sync_records", "ok", detail={"would_drop": len(gone), "dry_run": True})
            return
        for rec in gone:
            session.delete(rec)
        session.commit()
        steps.record(
            "sync_records",
            "ok",
            detail={"dropped": len(gone), "kept": len(records) - len(gone)},
        )


def last_gc_at(session: Session, datastore: str) -> datetime | None:
    """When the last GC that worked on ``datastore`` finished, from the cleanup runs."""
    runs = session.exec(
        select(PipelineRun)
        .where(PipelineRun.kind == PipelineKind.CLEANUP)
        .order_by(col(PipelineRun.started_at).desc())
        .limit(50)
    ).all()
    for run in runs:
        steps = run.steps or []
        ds = next((s["detail"].get("datastore") for s in steps if s.get("name") == "cleanup"), None)
        if ds != datastore:
            continue
        for step in steps:
            if step.get("name") == "gc" and step.get("status") == "ok":
                at = datetime.fromisoformat(step["finished_at"])
                return at if at.tzinfo else at.replace(tzinfo=UTC)
    return None


async def _pause(seconds: float, stop: asyncio.Event | None) -> None:
    if stop is None:
        await asyncio.sleep(seconds)
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
