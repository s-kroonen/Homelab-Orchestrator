"""Phase 6: run every :class:`BackupPolicy` on its cron schedule.

One APScheduler cron job per policy with a non-empty ``schedule_cron``. A
firing backs up each of the policy's services through the same
:class:`BackupPipeline` the CLI and API use (``actor="scheduler"``, integrity
gate on, PBS verify on), then — only after a *successful* run — enforces the
policy's retention via :func:`orchestrator.scheduler.retention.enforce_retention`.

Which services a policy covers is the union of:

* ``policy.targets["services"]`` — explicit slugs, and
* every service whose ``backup_policy`` names this policy in services.yaml.

Both spellings exist in the registry today (the example file uses the second
and leaves ``targets: {}``), so honouring only one would silently back up
nothing for half the configs out there.

Scheduled runs are serialised through one lock. This is a low-power homelab:
two vzdumps racing each other onto the same PBS is slower than running them
back to back, and two policies covering the same service must not dump it
twice at once.

The job set is rebuilt from the DB by :meth:`BackupScheduler.sync_jobs`, which
the policy API calls after every mutation — a dashboard edit takes effect
immediately, not at the next restart.
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable, TaskTimeout
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import BackupPolicy, CleanupPolicy, Service
from orchestrator.db.session import session_scope
from orchestrator.display import get_timezone
from orchestrator.domain.enums import AuditResult, BackupWakeMode, CleanupWhen
from orchestrator.logging_setup import get_logger
from orchestrator.notify.jobs import job_failed, job_ok
from orchestrator.pipelines.backup import BackupError, BackupPipeline, BackupSkipped, WakeContext
from orchestrator.scheduler.retention import RetentionError, enforce_retention

log = get_logger(__name__)

_JOB_PREFIX = "policy:"
_DUMP_PREFIX = "dump:"
_CLEANUP_PREFIX = "cleanup:"
_DUMP_SYNC_JOB = "dump-sync"


def job_id(policy_name: str) -> str:
    return f"{_JOB_PREFIX}{policy_name}"


def parse_cron(expr: str, timezone: Any = None) -> CronTrigger:
    """``CronTrigger.from_crontab`` with a uniform ``ValueError`` on bad input."""
    try:
        return CronTrigger.from_crontab(expr, timezone=timezone)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid cron expression {expr!r}: {exc}") from exc


def next_fire_time(expr: str, timezone: Any = None) -> datetime | None:
    """When ``expr`` would next fire, computed without a running scheduler."""
    if not expr:
        return None
    trigger = parse_cron(expr, timezone)
    now = datetime.now(trigger.timezone)
    return trigger.get_next_fire_time(None, now)  # type: ignore[no-any-return]


def resolve_targets(session: Session, policy: BackupPolicy) -> list[str]:
    """Slugs this policy backs up: explicit targets + services pointing at it."""
    explicit = list((policy.targets or {}).get("services") or [])
    attached: list[str] = []
    if policy.id is not None:
        attached = list(
            session.exec(select(Service.slug).where(Service.backup_policy_id == policy.id)).all()
        )
    # dict.fromkeys: dedupe, keep explicit-first order (an operator listing
    # slugs in targets is also stating the order they want them run in).
    slugs = list(dict.fromkeys([*explicit, *attached]))
    if not slugs:
        return slugs
    # A service marked backup_excluded (the VM hosting the PBS datastore, say) is never a
    # target, whatever a policy still says: the backup pipeline would only refuse it, every run.
    excluded = set(
        session.exec(
            select(Service.slug).where(
                col(Service.slug).in_(slugs), col(Service.backup_excluded).is_(True)
            )
        ).all()
    )
    for slug in excluded:
        log.warning("scheduler.target_excluded", policy=policy.name, service=slug)
    return [s for s in slugs if s not in excluded]


_JOB_SEQ = 0
#: The scheduler this process started (for callers without the app, like the MCP tools).
_CURRENT: BackupScheduler | None = None


def current() -> BackupScheduler | None:
    return _CURRENT


@dataclass
class QueuedJob:
    """A backup batch or a cleanup, waiting for or holding the queue."""

    id: int
    kind: str  # "backup" | "cleanup"
    name: str
    actor: str
    requires: frozenset[str]
    task: asyncio.Task[Any] | None = None
    state: str = "waiting"  # "waiting" | "running"
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    #: Set to stop a running cleanup's GC.
    stop: asyncio.Event = field(default_factory=asyncio.Event)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "actor": self.actor,
            "state": self.state,
            "requires": sorted(self.requires),
            "queued_at": self.queued_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


@dataclass
class _Carried:
    """The wake the queue carries from job to job."""

    ctx: WakeContext
    requires: frozenset[str]
    restore: bool
    pipeline: BackupPipeline
    since: datetime = field(default_factory=lambda: datetime.now(UTC))


def _cleanup_of(session: Session, policy: BackupPolicy) -> CleanupPolicy | None:
    if not policy.cleanup_policy:
        return None
    return session.exec(
        select(CleanupPolicy).where(CleanupPolicy.name == policy.cleanup_policy)
    ).one_or_none()


class BackupScheduler:
    def __init__(
        self,
        *,
        adapters: AdapterBundle,
        settings: Settings,
        pipeline_factory: Callable[[], BackupPipeline] | None = None,
    ) -> None:
        self._adapters = adapters
        self._settings = settings
        self._pipeline_factory = pipeline_factory or (
            lambda: BackupPipeline(
                proxmox=adapters.proxmox,
                pbs=adapters.pbs,
                settings=settings,
                power=adapters.power,
            )
        )
        self._scheduler = AsyncIOScheduler()
        #: Cron expressions run in this zone (the dashboard's timezone setting).
        self.timezone: Any = self._scheduler.timezone
        self._lock = asyncio.Lock()
        #: The jobs running and waiting, in order (see queue()).
        self._jobs: list[QueuedJob] = []
        #: What the queue holds awake between jobs.
        self._carried: _Carried | None = None
        self._background: set[asyncio.Task[Any]] = set()
        #: policy name -> why it has no job (bad cron). Shown on the dashboard.
        self.errors: dict[str, str] = {}
        #: cleanup policy name -> why it has no job (bad cron).
        self.cleanup_errors: dict[str, str] = {}
        #: service slug -> why its dump has no job (bad cron).
        self.dump_errors: dict[str, str] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start APScheduler and register jobs. Needs a running event loop."""
        global _CURRENT
        _CURRENT = self
        self._scheduler.start()
        self.sync_jobs()
        # Dump schedules live on services, which change without telling the scheduler: look again.
        self._scheduler.add_job(
            self.sync_dump_jobs,
            "interval",
            minutes=5,
            id=_DUMP_SYNC_JOB,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Deleted services leave the archive when their time is up.
        self._scheduler.add_job(
            self.purge_archive,
            "interval",
            hours=6,
            id="archive.purge",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        log.info("scheduler.started", jobs=len(self._scheduler.get_jobs()))

    async def shutdown(self) -> None:
        if self._scheduler.running:
            # wait=False: don't hold container shutdown hostage to a multi-hour
            # vzdump. PVE/PBS keep running the task; the PipelineRun stays
            # RUNNING, which is the honest state for "we stopped watching".
            self._scheduler.shutdown(wait=False)
            # APScheduler >=3.11 schedules the actual shutdown onto the loop
            # (call_soon_threadsafe); yield once so it has happened by the
            # time we return, rather than racing the loop's own teardown.
            await asyncio.sleep(0)
        log.info("scheduler.stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    # -- job set -------------------------------------------------------------

    def sync_jobs(self) -> None:
        """Rebuild the policy jobs from the DB. Idempotent; call after edits."""
        with session_scope() as session:
            policies = session.exec(select(BackupPolicy)).all()
            wanted = {p.name: p.schedule_cron.strip() for p in policies}
            self.timezone = get_timezone(session, self._settings)

        for job in self._scheduler.get_jobs():
            if job.id.startswith(_JOB_PREFIX) and job.id[len(_JOB_PREFIX) :] not in wanted:
                job.remove()

        self.errors = {}
        for name, cron in wanted.items():
            if not cron:
                self._remove(name)
                continue
            try:
                trigger = parse_cron(cron, self.timezone)
            except ValueError as exc:
                # One bad policy must not take the others' schedules down.
                self.errors[name] = str(exc)
                self._remove(name)
                log.error(
                    "scheduler.policy.invalid_cron",
                    policy=name,
                    cron=cron,
                    error=str(exc),
                    impact=f"Policy {name!r} will NOT run until its schedule is fixed.",
                )
                continue
            self._scheduler.add_job(
                self.run_policy,
                trigger,
                args=[name],
                id=job_id(name),
                name=f"backup policy {name}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                # A host that was asleep / restarting at 03:00 still runs the
                # nightly backup when it comes back within the hour.
                misfire_grace_time=3600,
            )
        self.sync_cleanup_jobs()
        self.sync_dump_jobs()
        log.info("scheduler.synced", policies=len(wanted), errors=len(self.errors))

    def sync_cleanup_jobs(self) -> None:
        """One job per cleanup policy with its own schedule. Idempotent."""
        with session_scope() as session:
            wanted = {
                c.name: c.schedule_cron.strip()
                for c in session.exec(select(CleanupPolicy)).all()
                if c.schedule_cron.strip()
            }
        for job in self._scheduler.get_jobs():
            if job.id.startswith(_CLEANUP_PREFIX) and job.id[len(_CLEANUP_PREFIX) :] not in wanted:
                job.remove()
        self.cleanup_errors = {}
        for name, cron in wanted.items():
            try:
                trigger = parse_cron(cron, self.timezone)
            except ValueError as exc:
                self.cleanup_errors[name] = str(exc)
                log.error("scheduler.cleanup.invalid_cron", cleanup=name, cron=cron, error=str(exc))
                continue
            self._scheduler.add_job(
                self.run_cleanup,
                trigger,
                args=[name],
                id=f"{_CLEANUP_PREFIX}{name}",
                name=f"cleanup policy {name}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

    def next_cleanup_time(self, name: str) -> datetime | None:
        job = self._scheduler.get_job(f"{_CLEANUP_PREFIX}{name}")
        return job.next_run_time if job is not None else None

    def purge_archive(self) -> None:
        from orchestrator.registry.archive import purge_expired

        try:
            with session_scope() as session:
                purge_expired(session)
        except Exception as exc:  # housekeeping must not take the scheduler down
            log.warning("archive.purge_failed", error=str(exc))

    def sync_dump_jobs(self) -> None:
        """One job per service whose ``dump:`` block has a schedule. Idempotent."""
        with session_scope() as session:
            wanted = {
                s.slug: str((s.dump or {}).get("schedule") or "").strip()
                for s in session.exec(select(Service)).all()
                if s.dump and (s.dump or {}).get("enabled", True)
            }
            self.timezone = get_timezone(session, self._settings)
        for job in self._scheduler.get_jobs():
            if job.id.startswith(_DUMP_PREFIX) and job.id[len(_DUMP_PREFIX) :] not in {
                slug for slug, cron in wanted.items() if cron
            }:
                job.remove()
        self.dump_errors = {}
        for slug, cron in wanted.items():
            if not cron:
                continue
            try:
                trigger = parse_cron(cron, self.timezone)
            except ValueError as exc:
                self.dump_errors[slug] = str(exc)
                log.error("scheduler.dump.invalid_cron", service=slug, cron=cron, error=str(exc))
                continue
            self._scheduler.add_job(
                self.run_dump,
                trigger,
                args=[slug],
                id=f"{_DUMP_PREFIX}{slug}",
                name=f"dump {slug}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

    async def run_dump(self, slug: str) -> None:
        """One firing of a service's dump schedule. Never raises (see run_policy)."""
        from orchestrator.dumps.pipeline import DumpPipeline, DumpRefused

        pipeline = DumpPipeline(self._adapters, self._settings)
        try:
            with session_scope() as session:
                await pipeline.run_for_service(session, slug, "scheduler")
        except DumpRefused as exc:
            log.info("scheduler.dump.skipped", service=slug, reason=str(exc))
        except Exception:
            log.exception("scheduler.dump.crashed", service=slug)

    def _remove(self, name: str) -> None:
        if self._scheduler.get_job(job_id(name)) is not None:
            self._scheduler.remove_job(job_id(name))

    def next_run_time(self, policy_name: str) -> datetime | None:
        job = self._scheduler.get_job(job_id(policy_name))
        return job.next_run_time if job is not None else None

    # -- the job itself ------------------------------------------------------

    # -- the queue -----------------------------------------------------------
    #
    # Backup batches and cleanups run one at a time, in the order they were asked for. What
    # they need woken (a policy's "needs": the storage the datastore lives on) is woken once
    # and carried from one job to the next while it covers them: the node is powered on once
    # for a cleanup and the backups after it, and put back after the last job that needs it.

    def queue(self) -> dict[str, Any]:
        """The jobs running and waiting, in order, and what the queue holds awake."""
        jobs = [j.as_dict() for j in self._jobs]
        held = (
            {
                "requires": sorted(self._carried.requires),
                "nodes": [h.node_name for h in self._carried.ctx.held],
                "since": self._carried.since.isoformat(),
            }
            if self._carried is not None
            else None
        )
        return {"held": held, "jobs": jobs}

    def cancel(self, job_id: int) -> str:
        """Cancel a waiting job, or stop a running cleanup's GC. Returns what was done."""
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            raise KeyError(job_id)
        if job.state == "waiting":
            if job.task is not None:
                job.task.cancel()
            return "cancelled"
        if job.kind == "cleanup":
            job.stop.set()
            return "stopping"
        raise ValueError("a running backup batch cannot be stopped from here")

    async def _queued(
        self, kind: str, name: str, actor: str, requires: list[str], body: Any
    ) -> Any:
        global _JOB_SEQ
        _JOB_SEQ += 1
        job = QueuedJob(
            id=_JOB_SEQ,
            kind=kind,
            name=name,
            actor=actor,
            requires=frozenset(requires),
            task=asyncio.current_task(),
        )
        self._jobs.append(job)
        try:
            async with self._lock:
                job.state = "running"
                job.started_at = datetime.now(UTC)
                try:
                    return await body(job)
                finally:
                    if job in self._jobs:
                        self._jobs.remove(job)
                    await self._hand_over()
        finally:
            if job in self._jobs:
                self._jobs.remove(job)
            if job.state == "waiting":
                # Cancelled before it ran: the job ahead may have kept the storage held for
                # it. Let the queue look again once it is free.
                task = asyncio.get_running_loop().create_task(self._release_if_idle())
                self._background.add(task)
                task.add_done_callback(self._background.discard)

    async def _release_if_idle(self) -> None:
        async with self._lock:
            await self._hand_over()

    async def _hand_over(self) -> None:
        """After a job: keep the carried wake for a waiting job it covers, else put it back."""
        carried = self._carried
        if carried is None:
            return
        if any(j.requires and j.requires <= carried.requires for j in self._jobs):
            return
        self._carried = None
        try:
            with session_scope() as session:
                await carried.pipeline.release_shared(session, carried.ctx, restore=carried.restore)
        except Exception as exc:  # never leave the queue wedged over a put-back
            log.error("scheduler.queue.release_failed", error=str(exc))

    async def _shared_wake(
        self,
        session: Session,
        pipeline: BackupPipeline,
        *,
        requires: list[str],
        wake_mode: BackupWakeMode,
        restore: bool,
        label: str,
        ttl_s: int,
        actor: str,
    ) -> WakeContext:
        """The wake a job runs under: the carried one when it covers ``requires`` (its holds
        renewed for this job), else a new one (the carried one put back first)."""
        need = frozenset(requires)
        carried = self._carried
        if carried is not None and need <= carried.requires and not carried.ctx.error:
            carried.restore = carried.restore and restore
            await carried.pipeline.renew_holds(session, carried.ctx, label=label, ttl_s=ttl_s)
            return carried.ctx
        # Even with nothing to wake the job gets a carried context: the runs in it hand over
        # what THEY woke (a node a service's backup powered on) to be put back at the end.
        if carried is not None:
            self._carried = None
            await carried.pipeline.release_shared(session, carried.ctx, restore=carried.restore)
        ctx = await pipeline.prepare_shared(
            session, requires=list(requires), wake_mode=wake_mode, label=label, actor=actor,
            ttl_s=ttl_s,
        )  # fmt: skip
        self._carried = _Carried(ctx=ctx, requires=need, restore=restore, pipeline=pipeline)
        return ctx

    # -- the jobs ------------------------------------------------------------

    async def run_policy(
        self, policy_name: str, *, actor: str = "scheduler"
    ) -> dict[str, dict[str, Any]]:
        """One firing: its cleanup before or after (when it has one), back up every target,
        prune the ones that succeeded. Queued behind whatever is running.

        Never raises — an exception escaping an APScheduler job is logged by
        APScheduler and then forgotten; every failure here is caught, logged
        with an impact line, and audited, then the next service runs.
        """
        with session_scope() as session:
            policy = session.exec(
                select(BackupPolicy).where(BackupPolicy.name == policy_name)
            ).one_or_none()
            if policy is None:
                log.warning("scheduler.policy.missing", policy=policy_name)
                return {}
            cleanup = _cleanup_of(session, policy)
            requires = list(
                dict.fromkeys(
                    [*(policy.requires or []), *((cleanup.requires or []) if cleanup else [])]
                )
            )
        return await self._queued(  # type: ignore[no-any-return]
            "backup", policy_name, actor, requires, lambda job: self._backup_job(policy_name, job)
        )

    async def run_cleanup(
        self, name: str, *, actor: str = "scheduler", reason: str = "its schedule"
    ) -> int | None:
        """Run a cleanup policy on its own, queued. Returns its run id (None: no such policy)."""
        with session_scope() as session:
            cleanup = session.exec(
                select(CleanupPolicy).where(CleanupPolicy.name == name)
            ).one_or_none()
            if cleanup is None:
                log.warning("scheduler.cleanup.missing", cleanup=name)
                return None
            requires = list(cleanup.requires or [])
        return await self._queued(  # type: ignore[no-any-return]
            "cleanup",
            name,
            actor,
            requires,
            lambda job: self._cleanup_job(name, job, actor, reason),
        )

    async def _cleanup_job(self, name: str, job: QueuedJob, actor: str, reason: str) -> int | None:
        with session_scope() as session:
            cleanup = session.exec(
                select(CleanupPolicy).where(CleanupPolicy.name == name)
            ).one_or_none()
            if cleanup is None:
                return None
            pipeline = self._pipeline_factory()
            return await self._run_cleanup(session, cleanup, pipeline, job, actor, reason)

    async def _run_cleanup(
        self,
        session: Session,
        cleanup: CleanupPolicy,
        pipeline: BackupPipeline,
        job: QueuedJob,
        actor: str,
        reason: str,
    ) -> int | None:
        from orchestrator.pipelines.cleanup import CleanupPipeline

        ctx = await self._shared_wake(
            session,
            pipeline,
            requires=list(cleanup.requires or []),
            wake_mode=cleanup.wake_mode,
            restore=cleanup.restore_power_state,
            label=f"cleanup:{cleanup.name}",
            ttl_s=self._cleanup_ttl_s(cleanup),
            actor=actor,
        )
        try:
            run = await CleanupPipeline(pbs=self._adapters.pbs, settings=self._settings).run(
                session, cleanup, prepared=ctx, actor=actor, reason=reason, stop=job.stop
            )
        except Exception as exc:
            log.error(
                "scheduler.cleanup.crashed",
                cleanup=cleanup.name,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            session.rollback()
            return None
        return run.id

    def _cleanup_ttl_s(self, cleanup: CleanupPolicy) -> int:
        return int(cleanup.max_wait_h * 3600 + self._settings.wake_timeout_s * 2 + 600)

    async def _datastore_full(self, cleanup: CleanupPolicy) -> bool:
        """Is the cleanup's datastore too full to back up into (the preflight's floor)?"""
        floor = self._settings.pbs_min_free_bytes
        if floor <= 0:
            return False
        try:
            status = await self._adapters.pbs.datastore_status(
                cleanup.datastore or self._settings.pbs_datastore
            )
        except Exception:
            return False
        return status.total_bytes > 0 and status.available_bytes < floor

    async def _backup_job(self, policy_name: str, job: QueuedJob) -> dict[str, dict[str, Any]]:
        with session_scope() as session:
            policy = session.exec(
                select(BackupPolicy).where(BackupPolicy.name == policy_name)
            ).one_or_none()
            if policy is None:
                return {}
            slugs = resolve_targets(session, policy)
            log.info("scheduler.policy.fire", policy=policy_name, services=slugs)
            pipeline = self._pipeline_factory()
            cleanup = _cleanup_of(session, policy)
            outcomes: dict[str, dict[str, Any]] = {}
            attempts = max(1, int(policy.max_attempts or 1))
            pending = list(slugs)
            ttl = pipeline.hold_ttl_s(len(pending))
            label = f"backup-policy:{policy.name}"

            ran_cleanup = False
            if cleanup is not None:
                # Wake what both need once, then decide: before, or after unless too full.
                both = list(dict.fromkeys([*(policy.requires or []), *(cleanup.requires or [])]))
                await self._shared_wake(
                    session, pipeline, requires=both, wake_mode=policy.wake_mode,
                    restore=policy.restore_power_state and cleanup.restore_power_state,
                    label=label, ttl_s=max(ttl, self._cleanup_ttl_s(cleanup)), actor="scheduler",
                )  # fmt: skip
                first = policy.cleanup_when is CleanupWhen.BEFORE
                why = f"before the backups of {policy.name}"
                if not first and await self._datastore_full(cleanup):
                    first, why = True, f"the datastore is too full for {policy.name}'s backups"
                if first:
                    await self._run_cleanup(session, cleanup, pipeline, job, "scheduler", why)
                    ran_cleanup = True

            for attempt in range(1, attempts + 1):
                if attempt > 1:
                    # Only what was blocked / not yet healthy comes back, after a
                    # pause - the other services in the batch are already done.
                    await asyncio.sleep(self._settings.backup_retry_delay_s)
                    session.expire_all()
                    log.info(
                        "scheduler.policy.retry",
                        policy=policy_name,
                        attempt=attempt,
                        of=attempts,
                        services=pending,
                    )
                # The policy's own prerequisites (e.g. the storage VM) are woken ONCE for the
                # whole batch - and for the jobs queued around it - not per service.
                prepared = await self._shared_wake(
                    session, pipeline, requires=list(policy.requires or []),
                    wake_mode=policy.wake_mode, restore=policy.restore_power_state,
                    label=label, ttl_s=pipeline.hold_ttl_s(len(pending)), actor="scheduler",
                )  # fmt: skip
                for slug in pending:
                    outcomes[slug] = await self._run_one(
                        session, policy, slug, pipeline, prepared, attempt, attempts
                    )
                pending = [s for s in pending if outcomes[s].get("retry")]
                if not pending:
                    break

            if cleanup is not None and not ran_cleanup:
                await self._run_cleanup(
                    session,
                    cleanup,
                    pipeline,
                    job,
                    "scheduler",
                    f"after the backups of {policy.name}",
                )
            return outcomes

    async def _run_one(
        self,
        session: Session,
        policy: BackupPolicy,
        slug: str,
        pipeline: BackupPipeline,
        prepared: WakeContext,
        attempt: int = 1,
        attempts: int = 1,
    ) -> dict[str, Any]:
        try:
            run = await pipeline.run_for_service(
                session,
                slug,
                actor="scheduler",
                verify=True,
                prepared=prepared,
                attempt=attempt,
                attempts=attempts,
            )
        except BackupSkipped as exc:
            # Not a failure: it is unhealthy, nothing can check it, or it is off and may not be
            # woken. The pipeline logged, audited and (unless it retries) announced it.
            log.info(
                "scheduler.backup.skipped",
                policy=policy.name,
                service=slug,
                why=exc.why,
                reason=str(exc),
                impact="No new backup; retention NOT applied, the last backup is kept.",
            )
            return {
                "backup": "skipped",
                "why": exc.why,
                "error": str(exc),
                "attempt": attempt,
                "retry": exc.retryable and attempt < attempts,
            }
        except (BackupError, AdapterError) as exc:
            retryable = isinstance(exc, AdapterUnreachable | TaskTimeout) or bool(
                getattr(exc, "retryable", False)
            )
            # The pipeline already recorded the run + audit row; retention is
            # deliberately skipped so a failure never prunes anything.
            log.error(
                "scheduler.backup.failed",
                policy=policy.name,
                service=slug,
                error=str(exc),
                impact="No new backup; retention NOT applied, existing snapshots untouched.",
            )
            return {
                "backup": "failed",
                "error": str(exc),
                "attempt": attempt,
                "retry": retryable and attempt < attempts,
            }
        except Exception as exc:
            log.error(
                "scheduler.backup.crashed",
                policy=policy.name,
                service=slug,
                error=str(exc),
                error_type=type(exc).__name__,
                traceback=traceback.format_exc(),
            )
            session.rollback()
            return {"backup": "crashed", "error": str(exc)}

        outcome: dict[str, Any] = {"backup": "succeeded", "run_id": run.id}
        try:
            result = await enforce_retention(
                session, self._adapters.pbs, policy, slug, settings=self._settings
            )
            session.commit()
            outcome["retention"] = result.as_dict()
            await job_ok(session, job=f"retention:{slug}", name=f"Pruning old backups of {slug}",
                         scope={"slug": slug})  # fmt: skip
        except (RetentionError, AdapterError, ValueError) as exc:
            session.rollback()
            log.error(
                "scheduler.retention.failed",
                policy=policy.name,
                service=slug,
                error=str(exc),
                impact="The backup succeeded, but old snapshots were not pruned/pinned.",
            )
            audit.record(
                session,
                actor="scheduler",
                action="backup.retention",
                target=f"service:{slug}",
                result=AuditResult.ERROR,
                details={"policy": policy.name, "error": str(exc)},
            )
            session.commit()
            await job_failed(
                session,
                job=f"retention:{slug}",
                name=f"Pruning old backups of {slug}",
                error=exc,
                impact=f"The backup itself worked; snapshots older than policy {policy.name!r} "
                "keeps stay in PBS until this works.",
                scope={"slug": slug},
            )
            outcome["retention"] = {"error": str(exc)}
        return outcome


__all__ = [
    "BackupScheduler",
    "current",
    "job_id",
    "next_fire_time",
    "parse_cron",
    "resolve_targets",
]
