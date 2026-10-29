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

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable, TaskTimeout
from orchestrator.adapters.pbs.base import PbsAdapter, Snapshot
from orchestrator.adapters.power.base import PowerAdapter
from orchestrator.adapters.proxmox.base import ProxmoxAdapter
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import (
    BackupPolicy,
    BackupRecord,
    Node,
    PipelineRun,
    Probe,
    Service,
)
from orchestrator.domain.enums import (
    AuditResult,
    BackupGate,
    BackupMode,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.health.engine import HealthEngine
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.backup_wake import BackupWaker, WakeContext
from orchestrator.pipelines.steps import (
    BackupError,
    BackupSkipped,
    RetryableBackupError,
    StepRecorder,
)
from orchestrator.pipelines.wake import WakePipeline
from orchestrator.registry.backup_plan import (
    BackupPlan,
    resolve_backup_plan,
    resolve_policy_prerequisites,
)
from orchestrator.registry.tree import TreeIndex, effective_guest

__all__ = ["BackupError", "BackupPipeline", "BackupSkipped", "StepRecorder", "WakeContext"]

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
        self._health = health or HealthEngine(settings=settings, proxmox=proxmox)
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
        attempt: int = 1,
        attempts: int = 1,
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

        ``attempt`` / ``attempts`` say which try of how many this is (a scheduled
        run retries a blocked or not-yet-healthy target). A retryable failure with
        tries left announces "will retry"; the last one announces the give-up.
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
            await self._precheck(session, steps, service, policy, gate=gate, wake=wake)
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
            await self._start_required_subs(session, steps, service, start=wake)
            await self._preflight(steps)
            await self._run_gate(session, steps, service, gate=gate)
            await self._run_sub_gate(session, steps, service, gate=gate)
            snapshot = await self._dump_and_locate(steps, service, node, policy)
            record = self._record_backup(session, steps, service, policy, run, snapshot)

            if verify:
                await self._verify(session, steps, snapshot, record)
            else:
                steps.record("pbs_verify", "skipped", detail={"reason": "verify=False"})

            await self._release_wake(
                session, steps, wake_ctx, restore=True, policy=policy, defer_to=prepared
            )
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

        except BackupSkipped as exc:
            will_retry = exc.retryable and attempt < attempts
            await self._release_wake(
                session, steps, wake_ctx, restore=True, policy=policy, defer_to=prepared
            )
            steps.record("skipped", "skipped", detail={"reason": str(exc), "why": exc.why})
            self._finish(session, run, PipelineStatus.SKIPPED, error=str(exc))
            log.info(
                "backup.skipped", service=slug, why=exc.why, will_retry=will_retry, reason=str(exc)
            )
            audit.record(
                session,
                actor=actor,
                action="backup.skipped",
                target=f"service:{slug}",
                correlation_id=correlation_id,
                details={
                    "run_id": run.id,
                    "why": exc.why,
                    "reason": str(exc),
                    "attempt": attempt,
                    "attempts": attempts,
                    "will_retry": will_retry,
                },
            )
            session.commit()
            if not will_retry:
                await announce(
                    session,
                    level="service",
                    key=service.slug,
                    name=service.name,
                    event="backup_skipped",
                    severity="info",
                    title=f"Backup of {service.name} skipped",
                    body=str(exc)[:1500],
                    scope={"slug": service.slug, "group": service.group},
                )
            raise
        except (BackupError, AdapterError) as exc:
            # AdapterUnreachable / TaskTimeout are *indeterminate*, not proof of
            # a bad backup — label them so phase 4's gate logic and the operator
            # can tell "we don't know" from "it failed".
            indeterminate = isinstance(exc, AdapterUnreachable | TaskTimeout)
            retryable = indeterminate or bool(getattr(exc, "retryable", False))
            will_retry = retryable and attempt < attempts
            await self._release_wake(
                session, steps, wake_ctx, restore=True, policy=policy, defer_to=prepared
            )
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
                    "attempt": attempt,
                    "attempts": attempts,
                    "will_retry": will_retry,
                },
            )
            session.commit()
            await self._announce_failure(
                session,
                service,
                exc,
                indeterminate=indeterminate,
                attempt=attempt,
                attempts=attempts,
                will_retry=will_retry,
            )
            raise
        finally:
            # Whatever happened — including an exception this method does not
            # handle or a cancelled request — never leave a node pinned awake.
            # Idempotent: the paths above already released on their way out.
            await self._release_wake(
                session, steps, wake_ctx, restore=True, policy=policy, defer_to=prepared
            )

    @staticmethod
    async def _announce_failure(
        session: Session,
        service: Service,
        exc: Exception,
        *,
        indeterminate: bool,
        attempt: int,
        attempts: int,
        will_retry: bool,
    ) -> None:
        scope = {"slug": service.slug, "group": service.group}
        if will_retry:
            await announce(
                session,
                level="service",
                key=service.slug,
                name=service.name,
                event="backup_retry",
                severity="warn",
                title=f"Backup of {service.name} blocked (try {attempt} of {attempts})",
                body=f"{str(exc)[:1300]}\n\nIt will be tried again shortly.",
                scope=scope,
            )
            return
        tries = f" after {attempt} tries" if attempts > 1 and attempt > 1 else ""
        gave_up = (
            " It will not be retried again until the next scheduled run." if attempts > 1 else ""
        )
        await announce(
            session,
            level="service",
            key=service.slug,
            name=service.name,
            event="backup_failed",
            severity="warn" if indeterminate else "error",
            title=f"Backup of {service.name} {'could not run' if indeterminate else 'FAILED'}{tries}",
            body=f"{str(exc)[:1300]}{gave_up if attempt >= attempts and attempts > 1 else ''}",
            scope=scope,
        )

    # -- before anything is woken ---------------------------------------------

    async def _precheck(
        self,
        session: Session,
        steps: StepRecorder,
        service: Service,
        policy: BackupPolicy | None,
        *,
        gate: bool,
        wake: bool,
    ) -> None:
        """Skip a backup that cannot be taken, before anything is woken for it:

        * nothing could verify it: no enabled, required check (on it or on a sub-service its
          backup waits for). The gate only lets a backup through once a check said healthy;
        * it is off and this backup may not wake it: its node is off (or its guest stopped)
          and the policy wakes nothing, or what would wake it is blocked (no power target, the
          node's wake on hit off). A prerequisite that cannot be woken is not a skip: that
          fails the run, since every backup of the policy is stuck behind it.
        """
        if gate and not self._has_required_check(session, service):
            steps.record("checks", "none", detail={"service": service.slug})
            raise BackupSkipped(
                f"{service.slug!r} has no required check, so nothing can verify it is healthy "
                f"before a backup. Add a required probe (or make one required) on its "
                f"Configuration tab; until then it is skipped and the last backup is kept.",
                why="no_checks",
            )
        if not wake:
            return
        plan = resolve_backup_plan(session, service, policy)
        target = next((s for s in plan.steps if s.is_target), None)
        if target is not None and target.blocker is None:
            return  # the backup wakes it if it has to
        if await self._target_up(session, service):
            return
        why = (
            target.blocker
            if target is not None
            else "this policy does not wake its target"
            + (
                " (wake mode never)"
                if plan.wake_mode == "never"
                else " (Wake before backup is off)"
            )
        )
        node = self._node_of(session, service)
        steps.record("target", "off", detail={"node": node.name if node else "", "why": why})
        raise BackupSkipped(
            f"{service.slug!r} is off and may not be woken for its backup: {why}. Skipped; the "
            f"last backup is kept.",
            why="unwakeable",
        )

    def _has_required_check(self, session: Session, service: Service) -> bool:
        subs = self._required_subs(session, service)
        return all(self._health.can_verify(session, s) for s in [service, *subs])

    @staticmethod
    def _node_of(session: Session, service: Service) -> Node | None:
        from orchestrator.pipelines.wake import node_of

        return node_of(session, service)

    async def _target_up(self, session: Session, service: Service) -> bool:
        """Its node online and its guest (if any) running. Never raises: not knowing counts as
        up, so a blip does not turn into a skip - the gate after it still decides."""
        node = self._node_of(session, service)
        if node is None:
            return False
        try:
            cluster = await self._proxmox.cluster_status()
            if not any(n.name == node.name and n.online for n in cluster.nodes):
                return False
            eg = effective_guest(session, service)
            if eg.kind is GuestKind.NONE or eg.guest_id is None:
                return True
            guests = await self._proxmox.list_guests(node.name)
        except AdapterError:
            return True
        guest = next((g for g in guests if g.vmid == eg.guest_id), None)
        return bool(guest and guest.status == "running")

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

    async def _release_wake(
        self,
        session: Session,
        steps: StepRecorder,
        ctx: WakeContext,
        *,
        restore: bool = False,
        policy: BackupPolicy | None = None,
        defer_to: WakeContext | None = None,
    ) -> None:
        """Release the holds and, when the policy says so, put back what the backup
        changed (guests it started, nodes it powered on). Idempotent."""
        if self._waker is None:
            return
        if ctx.held:
            await self._waker.release(session, ctx, steps)
        if restore and ctx.prior and (policy is None or policy.restore_power_state):
            await self._waker.restore(session, ctx, steps, defer_to=defer_to)
        elif restore and ctx.prior:
            ctx.prior.clear()

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

    async def release_prepared(
        self, session: Session, ctx: WakeContext, policy: BackupPolicy
    ) -> None:
        if self._waker is not None and ctx.held:
            await self._waker.release(session, ctx, None)
        if self._waker is not None and ctx.prior and policy.restore_power_state:
            await self._waker.restore(session, ctx, None)
        elif ctx.prior:
            ctx.prior.clear()

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

        * DOWN    — the guest gate: the node is offline or the guest is not
          running, so no probe ran. Refused, but never a restore candidate: a
          stopped VM says nothing about its data. Wake before backup (which runs
          before this gate) is what turns it into HEALTHY. The engine reads the
          guest state fresh for every gate check — never a cached cycle's.

        This asymmetry is the entire point of the model: never overwrite a good
        backup with a bad one, and never mistake "don't know" for either answer.
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
                {"name": r.probe_name, "kind": str(r.kind), "state": r.state.value}
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

        if verdict.state is HealthState.DOWN:
            # The guest gate: node offline or guest not running. Always
            # required for a backup — there is nothing consistent to dump
            # from a guest that is not running, and the probes never ran.
            raise RetryableBackupError(
                f"integrity gate refused {service.slug!r}: the guest is not running "
                f"(DOWN) — {verdict.reason.split(' — ', 1)[0]}. The guest is stopped or "
                f"its node is off, so its health could not be checked and no backup was "
                f"taken; the last known-good backup is untouched. This is NOT a restore "
                f"candidate. Turn on 'Wake before backup' in the backup policy (or start "
                f"the guest) so the backup brings it up first."
            )
        if verdict.state is HealthState.FAILED:
            raise BackupSkipped(
                f"integrity gate refused {service.slug!r}: the service is FAILED — "
                f"{verdict.reason}. Skipped: the last known-good backup is untouched. This "
                f"service is a restore candidate.",
                why="unhealthy",
            )
        raise BackupSkipped(
            f"integrity gate refused {service.slug!r}: health could not be established "
            f"(UNKNOWN) — {verdict.reason}. Skipped (failing closed): the last known-good "
            f"backup is untouched. This is not evidence of corruption, only that the check "
            f"could not run.",
            retryable=True,
            why="unknown",
        )

    # -- sub-services ----------------------------------------------------------

    @staticmethod
    def _required_subs(session: Session, service: Service) -> list[Service]:
        """Sub-services whose root's backup waits for them (backup_gate: required),
        enabled all the way up."""
        tree = TreeIndex.build(session)
        return [
            d
            for d in tree.descendants(service)
            if d.backup_gate is BackupGate.REQUIRED and tree.is_served(d)
        ]

    @staticmethod
    def _waited_checks(session: Session, service: Service) -> list[tuple[Service, set[str]]]:
        """Sub-services that are not ``backup_gate: required`` but have checks a backup
        waits for (``Probe.backup_waits``: a stack's database container), served all the
        way up, with the names of those checks."""
        tree = TreeIndex.build(session)
        out: list[tuple[Service, set[str]]] = []
        for d in tree.descendants(service):
            if d.backup_gate is BackupGate.REQUIRED or not tree.is_served(d):
                continue
            rows = session.exec(select(Probe).where(Probe.service_id == d.id)).all()
            names = {p.name for p in rows if p.backup_waits and p.enabled}
            if names:
                out.append((d, names))
        return out

    @staticmethod
    def _passes(verdict: ServiceVerdict, names: set[str] | None) -> bool:
        """The whole verdict HEALTHY (``names`` None), or each named check healthy."""
        if names is None:
            return verdict.state is HealthState.HEALTHY
        if verdict.state is HealthState.DOWN:
            return False
        got = {r.probe_name: r.state for r in verdict.probe_results}
        return all(got.get(n) is HealthState.HEALTHY for n in names)

    def _gated_subs(
        self, session: Session, service: Service
    ) -> list[tuple[Service, set[str] | None]]:
        """Everything below ``service`` its backup waits for: whole sub-services (None) and
        single checks on the others."""
        whole: list[tuple[Service, set[str] | None]] = [
            (s, None) for s in self._required_subs(session, service)
        ]
        return whole + [(s, n) for s, n in self._waited_checks(session, service)]

    async def _start_required_subs(
        self, session: Session, steps: StepRecorder, service: Service, *, start: bool
    ) -> None:
        """Bring up the sub-services a backup waits for that have a controller and are not
        running (the guest is up by now), then give them time to turn HEALTHY."""
        gated = self._gated_subs(session, service)
        if not gated:
            return
        waits = {s.slug: names for s, names in gated}
        subs = [s for s, _ in gated]
        from orchestrator.controllers import ControllerError, build_controller

        deadline = time.monotonic() + self._settings.wake_timeout_s
        for sub in subs:
            started = datetime.now(UTC)
            if not start:
                steps.record(
                    f"sub_start:{sub.slug}", "skipped", detail={"reason": "wake=False override"}
                )
                continue
            if not sub.controller:
                steps.record(
                    f"sub_start:{sub.slug}",
                    "skipped",
                    detail={"reason": "no controller to start it"},
                )
                continue
            try:
                ctl = build_controller(self._settings, self._proxmox, session, sub)
                assert ctl is not None
                if not (await ctl.status()).running:
                    message = await ctl.start()
                    steps.record(
                        f"sub_start:{sub.slug}",
                        "ok",
                        started_at=started,
                        detail={"controller": ctl.describe(), "message": message},
                    )
                else:
                    steps.record(
                        f"sub_start:{sub.slug}",
                        "already_running",
                        detail={"controller": ctl.describe()},
                    )
            except (ControllerError, ValueError, AssertionError) as exc:
                raise RetryableBackupError(
                    f"required sub-service {sub.slug!r} could not be started through its "
                    f"controller: {exc}"
                ) from exc
            # Wait for it to be judged HEALTHY (a container needs a moment).
            while True:
                verdict = await self._health.scan(session, sub)
                if self._passes(verdict, waits[sub.slug]) or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(self._settings.wake_poll_interval_s)

    async def _run_sub_gate(
        self, session: Session, steps: StepRecorder, service: Service, *, gate: bool
    ) -> None:
        """The root's backup also waits for its required sub-services: each must be
        HEALTHY. One that is not refuses the whole backup, like the root's own gate."""
        if not gate:
            return
        for sub, names in self._gated_subs(session, service):
            started = datetime.now(UTC)
            verdict = await self._health.scan(session, sub)
            detail: dict[str, Any] = {"verdict": verdict.state.value, "reason": verdict.reason}
            if names is not None:
                detail["checks"] = sorted(names)
            if self._passes(verdict, names):
                steps.record(f"sub_gate:{sub.slug}", "ok", started_at=started, detail=detail)
                continue
            if names is not None:
                await self._refuse_checks(steps, service, sub, names, verdict, started, detail)
            steps.record(
                f"sub_gate:{sub.slug}",
                "failed" if verdict.state is HealthState.FAILED else "indeterminate",
                started_at=started,
                detail=detail,
            )
            how = {
                HealthState.FAILED: "is FAILED",
                HealthState.DOWN: "is not running",
                HealthState.UNKNOWN: "could not be verified (UNKNOWN)",
            }.get(verdict.state, verdict.state.value)
            message = (
                f"integrity gate refused {service.slug!r}: its required sub-service "
                f"{sub.slug!r} {how} - {verdict.reason}. No backup was taken and the last "
                f"known-good backup is untouched. (Set {sub.slug!r} to backup_gate: ignore "
                f"if a backup should not wait for it.)"
            )
            if verdict.state is HealthState.DOWN:
                raise RetryableBackupError(message)  # it was started and did not come up
            raise BackupSkipped(
                message,
                retryable=verdict.state is HealthState.UNKNOWN,
                why="sub_unhealthy",
            )

    @staticmethod
    async def _refuse_checks(
        steps: StepRecorder,
        service: Service,
        sub: Service,
        names: set[str],
        verdict: ServiceVerdict,
        started: datetime,
        detail: dict[str, Any],
    ) -> None:
        """A check a backup waits for (``backup_waits``) on ``sub`` did not pass."""
        got = {r.probe_name: r for r in verdict.probe_results}
        bad = sorted(
            n for n in names if (r := got.get(n)) is None or r.state is not HealthState.HEALTHY
        )
        failed = any(got[n].state is HealthState.FAILED for n in bad if n in got)
        steps.record(
            f"sub_gate:{sub.slug}",
            "failed" if failed else "indeterminate",
            started_at=started,
            detail={**detail, "not_ok": bad},
        )
        why = "; ".join(f"{n}: {got[n].message}" if n in got else f"{n}: did not run" for n in bad)
        message = (
            f"integrity gate refused {service.slug!r}: {sub.slug!r} has checks its backup waits "
            f"for that did not pass - {why}. No backup was taken and the last known-good backup "
            f"is untouched. (Untick 'backup waits' on a check if a backup should not wait for it.)"
        )
        if verdict.state is HealthState.DOWN:
            raise RetryableBackupError(message)
        raise BackupSkipped(message, retryable=not failed, why="sub_unhealthy")

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
        if service.parent_id is not None:
            root = effective_guest(session, service).root_slug
            raise BackupError(
                f"{slug!r} is a sub-service: backups are per guest, so back up its "
                f"root service {root!r} (mark this one backup_gate: required to make "
                f"that backup wait for it)"
            )
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
