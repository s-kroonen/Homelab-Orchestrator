"""Backup pipeline.

    resolve service -> WAKE prerequisites + target (hold their nodes)
    -> preflight PBS -> INTEGRITY GATE -> vzdump -> poll task
    -> locate snapshot -> record it -> PBS verify -> update record
    -> release the holds (always, even on failure)

The gate is the reason this project exists. Only a HEALTHY verdict proceeds;
FAILED and UNKNOWN both abort with the last known-good backup untouched. See
:meth:`BackupPipeline._run_gate`.

The wake phase comes BEFORE the PBS preflight because the PBS datastore may live
on storage (a TrueNAS VM on a normally-off node) that has to be up first. It
resolves what to wake from :mod:`orchestrator.registry.backup_plan` and does the
waking through :class:`WakePipeline` — see :mod:`orchestrator.pipelines.backup_wake`
and docs/backup_wake.md. A wake that fails fails the run at that step: the gate
and the dump never run, exactly like a gate refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable, TaskTimeout
from orchestrator.adapters.pbs.base import PbsAdapter, Snapshot
from orchestrator.adapters.power.base import PowerAdapter
from orchestrator.adapters.proxmox.base import ProxmoxAdapter
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import BackupPolicy, BackupRecord, Node, PipelineRun, Service
from orchestrator.domain.enums import (
    AuditResult,
    BackupMode,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.health.engine import HealthEngine
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.pipelines.backup_wake import BackupWaker, WakeContext
from orchestrator.pipelines.steps import BackupError, StepRecorder
from orchestrator.pipelines.wake import WakePipeline
from orchestrator.registry.backup_plan import (
    BackupPlan,
    resolve_backup_plan,
    resolve_policy_prerequisites,
)

__all__ = ["BackupError", "BackupPipeline", "StepRecorder", "WakeContext"]

log = get_logger(__name__)

# PBS backup-type strings, keyed by our guest kind.
_KIND_TO_PBS_TYPE: dict[GuestKind, str] = {
    GuestKind.VM: "vm",
    GuestKind.CT: "ct",
}


class BackupPipeline:
    def __init__(
        self,
        *,
        proxmox: ProxmoxAdapter,
        pbs: PbsAdapter,
        settings: Settings,
        health: HealthEngine | None = None,
        power: PowerAdapter | None = None,
        wake_pipeline: WakePipeline | None = None,
    ) -> None:
        self._proxmox = proxmox
        self._pbs = pbs
        self._settings = settings
        # Injected so tests can drive the gate deterministically. Defaults to a
        # real engine — the gate must be on unless someone explicitly opts out.
        self._health = health or HealthEngine(settings=settings)
        # The wake phase needs the power adapter. Every real entry point (API,
        # CLI, scheduler) passes it; without one there is nothing to wake WITH,
        # so the phase is skipped (and logged) rather than guessed at — which
        # keeps a bare BackupPipeline(proxmox=, pbs=, settings=) behaving as it
        # did before backups could wake anything.
        if wake_pipeline is None and power is not None:
            wake_pipeline = WakePipeline(
                power=power, proxmox=proxmox, settings=settings, health=self._health
            )
        self._waker = BackupWaker(wake_pipeline, settings) if wake_pipeline else None

    async def run_for_service(
        self,
        session: Session,
        slug: str,
        *,
        actor: str = "system",
        verify: bool = True,
        gate: bool = True,
        wake: bool = True,
        prepared: WakeContext | None = None,
    ) -> PipelineRun:
        """Back up one service. Returns the completed :class:`PipelineRun`.

        ``gate=False`` bypasses the integrity gate. It exists for deliberate
        operator override and is audited as such — it is never the default, and
        the caller has to ask for it explicitly.

        ``wake=False`` skips the wake phase (prerequisites AND target) — for an
        operator who has brought everything up by hand. Recorded as a skipped
        step and audited as an explicit override, like ``gate=False``.

        ``prepared`` is a :class:`WakeContext` from :meth:`prepare_policy`: the
        policy-level prerequisites a scheduled batch already woke and holds, which
        this run must not wake (or release) again.
        """
        correlation_id = new_correlation_id()
        service, node, policy = self._resolve(session, slug)

        run = PipelineRun(
            kind=PipelineKind.BACKUP,
            correlation_id=correlation_id,
            service_id=service.id,
            status=PipelineStatus.RUNNING,
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        steps = StepRecorder(session, run)
        audit.record(
            session,
            actor=actor,
            action="backup.start",
            target=f"service:{slug}",
            correlation_id=correlation_id,
            details={"run_id": run.id, "node": node.name, "vmid": service.guest_id},
        )
        session.commit()

        wake_ctx = WakeContext()
        try:
            await self._wake_phase(
                session,
                steps,
                service,
                policy,
                wake=wake,
                shared=prepared,
                ctx=wake_ctx,
                actor=actor,
                correlation_id=correlation_id,
                run_id=run.id,
            )
            await self._preflight(steps)
            await self._run_gate(session, steps, service, gate=gate)
            snapshot = await self._dump_and_locate(steps, service, node, policy)
            record = self._record_backup(session, steps, service, policy, run, snapshot)

            if verify:
                await self._verify(session, steps, snapshot, record)
            else:
                steps.record("pbs_verify", "skipped", detail={"reason": "verify=False"})

            await self._release_wake(session, steps, wake_ctx)
            self._finish(session, run, PipelineStatus.SUCCEEDED)
            audit.record(
                session,
                actor=actor,
                action="backup.succeeded",
                target=f"service:{slug}",
                correlation_id=correlation_id,
                details={"run_id": run.id, "snapshot": snapshot.snapshot_id},
            )
            session.commit()
            return run

        except (BackupError, AdapterError) as exc:
            # AdapterUnreachable / TaskTimeout are *indeterminate*, not proof of
            # a bad backup — label them so phase 4's gate logic and the operator
            # can tell "we don't know" from "it failed".
            indeterminate = isinstance(exc, AdapterUnreachable | TaskTimeout)
            await self._release_wake(session, steps, wake_ctx)
            steps.record(
                "aborted",
                "indeterminate" if indeterminate else "failed",
                detail={"error": str(exc), "type": type(exc).__name__},
            )
            self._finish(session, run, PipelineStatus.FAILED, error=str(exc))
            audit.record(
                session,
                actor=actor,
                action="backup.failed",
                target=f"service:{slug}",
                result=AuditResult.ERROR,
                correlation_id=correlation_id,
                details={
                    "run_id": run.id,
                    "error": str(exc),
                    "indeterminate": indeterminate,
                },
            )
            session.commit()
            raise
        finally:
            # Whatever happened — including an exception this method does not
            # handle or a cancelled request — never leave a node pinned awake.
            # Idempotent: the paths above already released on their way out.
            await self._release_wake(session, steps, wake_ctx)

    # -- wake phase ----------------------------------------------------------

    async def _wake_phase(
        self,
        session: Session,
        steps: StepRecorder,
        service: Service,
        policy: BackupPolicy | None,
        *,
        wake: bool,
        shared: WakeContext | None,
        ctx: WakeContext,
        actor: str,
        correlation_id: str,
        run_id: int | None,
    ) -> None:
        plan = resolve_backup_plan(session, service, policy)
        if not plan.steps:
            return
        if not wake:
            self._record_wake_skipped(session, steps, service, plan, actor, correlation_id, run_id)
            return
        if self._waker is None:
            log.info("backup.wake.unavailable", service=service.slug, reason="no power adapter")
            return
        await self._waker.run(
            session,
            plan.steps,
            ctx,
            steps,
            actor=actor,
            hold_reason=f"backup:{service.slug}",
            ttl_s=self._waker.hold_ttl_s(),
            shared=shared,
        )

    @staticmethod
    def _record_wake_skipped(
        session: Session,
        steps: StepRecorder,
        service: Service,
        plan: BackupPlan,
        actor: str,
        correlation_id: str,
        run_id: int | None,
    ) -> None:
        # Explicit operator override, like gate=False: loud, recorded, audited.
        would_wake = [s.slug for s in plan.steps]
        log.warning(
            "backup.wake.skipped",
            service=service.slug,
            impact="Nothing was woken; the backup runs against whatever is up.",
        )
        steps.record(
            "wake_prerequisites",
            "skipped",
            detail={"reason": "caller passed wake=False", "would_wake": would_wake},
        )
        audit.record(
            session,
            actor=actor,
            action="backup.wake_skipped",
            target=f"service:{service.slug}",
            correlation_id=correlation_id,
            details={"run_id": run_id, "would_wake": would_wake},
        )
        session.commit()

    async def _release_wake(self, session: Session, steps: StepRecorder, ctx: WakeContext) -> None:
        if self._waker is not None and ctx.held:
            await self._waker.release(session, ctx, steps)

    async def prepare_policy(
        self, session: Session, policy: BackupPolicy, *, actor: str = "scheduler", services: int = 1
    ) -> WakeContext:
        """Wake a policy's own prerequisites ONCE for a whole scheduled batch and
        hold them until :meth:`release_prepared`. Never raises: a failure is kept
        on the returned context, and every per-service run handed that context
        fails at that step with the same clear error (and none re-fires the wake).
        """
        ctx = WakeContext()
        if self._waker is None:
            return ctx
        entries = resolve_policy_prerequisites(session, policy)
        if not entries:
            return ctx
        try:
            await self._waker.run(
                session,
                entries,
                ctx,
                None,
                actor=actor,
                hold_reason=f"backup-policy:{policy.name}",
                ttl_s=self._waker.hold_ttl_s(services),
            )
        except BackupError as exc:
            log.error(
                "backup.policy.prerequisites_failed",
                policy=policy.name,
                error=str(exc),
                impact="Every backup in this policy run fails at the wake step.",
            )
        return ctx

    async def release_prepared(self, session: Session, ctx: WakeContext) -> None:
        if self._waker is not None and ctx.held:
            await self._waker.release(session, ctx, None)

    # -- steps ---------------------------------------------------------------

    async def _preflight(self, steps: StepRecorder) -> None:
        """Confirm PBS is actually reachable before we make PVE do work.

        A real check, not a ping: an authenticated datastore-status call, so a
        bad token or a wrong datastore name fails here rather than halfway
        through a multi-hour dump.
        """
        started = datetime.now(UTC)
        status = await self._pbs.datastore_status(self._settings.pbs_datastore)
        steps.record(
            "pbs_preflight",
            "ok",
            started_at=started,
            detail={
                "datastore": status.name,
                "used_bytes": status.used_bytes,
                "available_bytes": status.available_bytes,
            },
        )

    async def _run_gate(
        self,
        session: Session,
        steps: StepRecorder,
        service: Service,
        *,
        gate: bool = True,
    ) -> None:
        """The integrity gate. **HEALTHY is the only state that proceeds.**

        FAILED and UNKNOWN both abort, and they abort for different reasons that
        the caller downstream needs to distinguish:

        * FAILED  — the service is definitively broken. Backing it up now would
          capture that broken state over a known-good backup. It is also a
          restore candidate.
        * UNKNOWN — we could not establish that it is healthy. Fail closed. A
          network blip must not be allowed to produce a backup we would later
          trust, but it must equally not be treated as corruption.

        This asymmetry is the entire point of the three-state model: never
        overwrite a good backup with a bad one, and never mistake "don't know"
        for either answer.
        """
        if not gate:
            # Explicit operator override. Loud, audited, and never the default.
            log.warning(
                "backup.gate.bypassed",
                service=service.slug,
                impact="This backup was NOT verified healthy before being taken.",
            )
            steps.record(
                "integrity_gate",
                "bypassed",
                detail={"ungated": True, "reason": "caller passed gate=False"},
            )
            return

        started = datetime.now(UTC)
        verdict = await self._health.scan(session, service)
        detail = {
            "verdict": verdict.state.value,
            "reason": verdict.reason,
            "probe_results": [
                {"name": r.probe_name, "kind": r.kind.value, "state": r.state.value}
                for r in verdict.probe_results
            ],
        }

        if verdict.state is HealthState.HEALTHY:
            steps.record("integrity_gate", "ok", started_at=started, detail=detail)
            return

        steps.record(
            "integrity_gate",
            "failed" if verdict.state is HealthState.FAILED else "indeterminate",
            started_at=started,
            detail=detail,
        )

        if verdict.state is HealthState.FAILED:
            raise BackupError(
                f"integrity gate refused {service.slug!r}: the service is FAILED — "
                f"{verdict.reason}. The last known-good backup is untouched. "
                f"This service is a restore candidate."
            )
        raise BackupError(
            f"integrity gate refused {service.slug!r}: health could not be "
            f"established (UNKNOWN) — {verdict.reason}. Failing closed: no backup "
            f"was taken and the last known-good backup is untouched. This is not "
            f"evidence of corruption, only that the check could not run."
        )

    async def _dump_and_locate(
        self,
        steps: StepRecorder,
        service: Service,
        node: Node,
        policy: BackupPolicy | None,
    ) -> Snapshot:
        if service.guest_id is None:
            raise BackupError(f"service {service.slug!r} has no guest_id to back up")
        pbs_type = _KIND_TO_PBS_TYPE.get(service.guest_kind)
        if pbs_type is None:
            raise BackupError(
                f"service {service.slug!r} has guest_kind={service.guest_kind.value!r}, "
                "which has no Proxmox guest to dump"
            )

        mode = (policy.mode if policy else BackupMode.SNAPSHOT).value

        started = datetime.now(UTC)
        handle = await self._proxmox.vzdump(
            node=node.name,
            vmid=service.guest_id,
            storage=self._settings.pve_backup_storage,
            mode=mode,
            notes=f"orchestrator: {service.slug}",
        )
        steps.record(
            "vzdump_started",
            "ok",
            started_at=started,
            detail={"upid": handle.upid, "node": node.name, "vmid": service.guest_id},
        )

        started = datetime.now(UTC)
        status = await self._proxmox.wait_for_task(
            node.name, handle.upid, timeout_s=self._settings.backup_task_timeout_s
        )
        if not status.succeeded:
            steps.record(
                "vzdump_finished",
                "failed",
                started_at=started,
                detail={"upid": handle.upid, "exit_status": status.exit_status},
            )
            raise BackupError(
                f"vzdump for {service.slug} failed: {status.exit_status or 'unknown'}"
            )
        steps.record(
            "vzdump_finished",
            "ok",
            started_at=started,
            detail={"upid": handle.upid, "exit_status": status.exit_status},
        )

        # Locate the snapshot the dump just produced: newest in the group.
        started = datetime.now(UTC)
        snapshots = await self._pbs.list_snapshots(
            self._settings.pbs_datastore,
            backup_type=pbs_type,
            backup_id=str(service.guest_id),
        )
        if not snapshots:
            raise BackupError(
                f"vzdump reported OK but PBS shows no snapshot for "
                f"{pbs_type}/{service.guest_id} in datastore "
                f"{self._settings.pbs_datastore!r} — check that PVE storage "
                f"{self._settings.pve_backup_storage!r} really targets that datastore"
            )
        snapshot = snapshots[0]
        steps.record(
            "snapshot_located",
            "ok",
            started_at=started,
            detail={"snapshot": snapshot.snapshot_id, "size_bytes": snapshot.size_bytes},
        )
        return snapshot

    def _record_backup(
        self,
        session: Session,
        steps: StepRecorder,
        service: Service,
        policy: BackupPolicy | None,
        run: PipelineRun,
        snapshot: Snapshot,
    ) -> BackupRecord:
        record = BackupRecord(
            service_id=service.id,
            service_slug=service.slug,
            policy_id=policy.id if policy else None,
            pipeline_run_id=run.id,
            pbs_snapshot_id=snapshot.snapshot_id,
            size_bytes=snapshot.size_bytes,
            verified=False,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        steps.record("backup_recorded", "ok", detail={"backup_record_id": record.id})
        return record

    async def _verify(
        self,
        session: Session,
        steps: StepRecorder,
        snapshot: Snapshot,
        record: BackupRecord,
    ) -> None:
        started = datetime.now(UTC)
        verified = await self._pbs.verify_snapshot(
            snapshot.datastore,
            backup_type=snapshot.backup_type,
            backup_id=snapshot.backup_id,
            backup_time=snapshot.backup_time,
            timeout_s=self._settings.verify_task_timeout_s,
        )
        record.verified = verified
        record.verified_at = datetime.now(UTC) if verified else None
        session.add(record)
        session.commit()

        steps.record(
            "pbs_verify",
            "ok" if verified else "failed",
            started_at=started,
            detail={"snapshot": snapshot.snapshot_id, "verified": verified},
        )
        if not verified:
            raise BackupError(
                f"PBS verify failed for {snapshot.snapshot_id} — the backup exists "
                "but its integrity is not confirmed"
            )

    # -- helpers -------------------------------------------------------------

    def _resolve(self, session: Session, slug: str) -> tuple[Service, Node, BackupPolicy | None]:
        service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        if service is None:
            raise BackupError(f"no service registered with slug {slug!r}")
        if not service.enabled:
            raise BackupError(f"service {slug!r} is disabled in the registry")
        if service.backup_excluded:
            # Deliberately unconditional — no force flag, no CLI override. The
            # motivating case (a VM hosting the pool PBS lives on) is never
            # correct to back up this way, so it must not be one keystroke away.
            reason = service.backup_excluded_reason or "no reason recorded"
            raise BackupError(
                f"service {slug!r} is marked backup_excluded and will not be backed "
                f"up: {reason}. This is a hard exclusion with no override — if the "
                f"exclusion is wrong, change backup_excluded in services.yaml."
            )

        node = session.get(Node, service.node_id) if service.node_id else None
        if node is None:
            raise BackupError(f"service {slug!r} is not attached to a node")

        policy = (
            session.get(BackupPolicy, service.backup_policy_id)
            if service.backup_policy_id
            else None
        )
        return service, node, policy

    @staticmethod
    def _finish(
        session: Session,
        run: PipelineRun,
        status: PipelineStatus,
        *,
        error: str | None = None,
    ) -> None:
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.error = error
        session.add(run)
        session.commit()
