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
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable, TaskTimeout
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import BackupPolicy, Service
from orchestrator.db.session import session_scope
from orchestrator.display import get_timezone
from orchestrator.domain.enums import AuditResult
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.backup import BackupError, BackupPipeline, WakeContext
from orchestrator.scheduler.retention import RetentionError, enforce_retention

log = get_logger(__name__)

_JOB_PREFIX = "policy:"


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
        #: policy name -> why it has no job (bad cron). Shown on the dashboard.
        self.errors: dict[str, str] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start APScheduler and register jobs. Needs a running event loop."""
        self._scheduler.start()
        self.sync_jobs()
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
        log.info("scheduler.synced", policies=len(wanted), errors=len(self.errors))

    def _remove(self, name: str) -> None:
        if self._scheduler.get_job(job_id(name)) is not None:
            self._scheduler.remove_job(job_id(name))

    def next_run_time(self, policy_name: str) -> datetime | None:
        job = self._scheduler.get_job(job_id(policy_name))
        return job.next_run_time if job is not None else None

    # -- the job itself ------------------------------------------------------

    async def run_policy(self, policy_name: str) -> dict[str, dict[str, Any]]:
        """One firing: back up every target, prune the ones that succeeded.

        Never raises — an exception escaping an APScheduler job is logged by
        APScheduler and then forgotten; every failure here is caught, logged
        with an impact line, and audited, then the next service runs.
        """
        async with self._lock:
            with session_scope() as session:
                policy = session.exec(
                    select(BackupPolicy).where(BackupPolicy.name == policy_name)
                ).one_or_none()
                if policy is None:
                    log.warning("scheduler.policy.missing", policy=policy_name)
                    return {}
                slugs = resolve_targets(session, policy)
                log.info("scheduler.policy.fire", policy=policy_name, services=slugs)
                outcomes: dict[str, dict[str, Any]] = {}
                # The policy's own prerequisites (e.g. the storage VM) are woken
                # ONCE for the whole batch and held until the last service is
                # done — not woken and released per service. Per-service extras
                # and each target are still handled per service, in its own run.
                pipeline = self._pipeline_factory()
                attempts = max(1, int(policy.max_attempts or 1))
                pending = list(slugs)
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
                    prepared = await pipeline.prepare_policy(
                        session, policy, actor="scheduler", services=len(pending)
                    )
                    try:
                        for slug in pending:
                            outcomes[slug] = await self._run_one(
                                session, policy, slug, pipeline, prepared, attempt, attempts
                            )
                    finally:
                        await pipeline.release_prepared(session, prepared, policy)
                    pending = [s for s in pending if outcomes[s].get("retry")]
                    if not pending:
                        break
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
            outcome["retention"] = {"error": str(exc)}
        return outcome


__all__ = [
    "BackupScheduler",
    "job_id",
    "next_fire_time",
    "parse_cron",
    "resolve_targets",
]
