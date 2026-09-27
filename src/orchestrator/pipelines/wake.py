"""Wake pipeline.

    resolve service -> node + guest
    -> assert a hold (before any work, so nothing powers the node back off
       mid-wake — released again once this pipeline is done, success or not)
    -> publish power-on to the power manager, unless the node already reports
       online
    -> poll until the node itself is online in the Proxmox cluster
    -> start the guest, unless it is already running
    -> poll the health engine until HEALTHY or timeout
    -> release the hold

Split into :meth:`start` (fast, synchronous — creates the ``PipelineRun`` row)
and :meth:`execute` (the actual work, run detached from the request that
triggered it) because a cold node can legitimately take minutes to become
healthy, and the web layer that fires this must return immediately — see
``web/routers/wake.py``. ``run_for_service`` is the synchronous combination
of both, for the CLI and anything else that wants to just wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime

from sqlmodel import Session, desc, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable
from orchestrator.adapters.power.base import PowerAdapter
from orchestrator.adapters.proxmox.base import ClusterStatus, ProxmoxAdapter
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import Node, NodeState, PipelineRun, Service
from orchestrator.db.session import session_scope
from orchestrator.domain.enums import (
    AuditResult,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
    PowerState,
)
from orchestrator.health.engine import HealthEngine
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.pipelines.backup import StepRecorder

log = get_logger(__name__)


class WakeError(Exception):
    """Raised when a wake cannot proceed or did not complete in time."""


class WakePipeline:
    def __init__(
        self,
        *,
        power: PowerAdapter,
        proxmox: ProxmoxAdapter,
        settings: Settings,
        health: HealthEngine | None = None,
    ) -> None:
        self._power = power
        self._proxmox = proxmox
        self._settings = settings
        # Injected so tests can drive polling deterministically. Defaults to a
        # real engine, same reasoning as BackupPipeline.
        self._health = health or HealthEngine(settings=settings)

    # -- entry points ---------------------------------------------------------

    def get_or_start(
        self, session: Session, slug: str, *, actor: str = "system"
    ) -> tuple[PipelineRun, bool]:
        """Reuse the latest wake for this service instead of starting a new
        one, when either is true:

        * it's still RUNNING, or
        * it just ended (success or failure) and ``wake_retry_cooldown_s``
          hasn't passed yet.

        Returns ``(run, started_new)``.

        Both the ``POST /wake`` API and the maintenance page call this rather
        than :meth:`start` directly. The cooldown matters as much as the
        RUNNING check: without it, a wake that fails fast gets re-triggered by
        every visitor request that lands on it during an outage — Traefik's
        errors middleware routes ALL of them here, not just page reloads — so
        a real failure (say, a non-quorate cluster) would hammer the power
        manager with a fresh wake command every request instead of surfacing
        once and cooling down.
        """
        service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        if service is not None and service.id is not None:
            latest = session.exec(
                select(PipelineRun)
                .where(PipelineRun.service_id == service.id, PipelineRun.kind == PipelineKind.WAKE)
                .order_by(desc(PipelineRun.started_at))
                .limit(1)
            ).one_or_none()
            if latest is not None:
                if latest.status is PipelineStatus.RUNNING:
                    return latest, False
                if latest.finished_at is not None:
                    # finished_at is written as UTC (see _finish), but SQLite
                    # drops tzinfo on round-trip while an in-memory object
                    # (not yet re-fetched from the DB) still carries it.
                    # Normalize both sides to naive UTC before subtracting.
                    now = datetime.now(UTC).replace(tzinfo=None)
                    finished_at = latest.finished_at
                    if finished_at.tzinfo is not None:
                        finished_at = finished_at.astimezone(UTC).replace(tzinfo=None)
                    age_s = (now - finished_at).total_seconds()
                    if age_s < self._settings.wake_retry_cooldown_s:
                        return latest, False
        return self.start(session, slug, actor=actor), True

    def start(self, session: Session, slug: str, *, actor: str = "system") -> PipelineRun:
        """Resolve + validate the request and create the RUNNING run row.

        Synchronous, DB-only — fast enough to await inline from a request
        handler before handing the rest off to :meth:`execute`. Raises
        :class:`WakeError` for anything the caller should see immediately
        (unknown slug, disabled service) rather than discovering it later in
        a background task nobody is watching.
        """
        correlation_id = new_correlation_id()
        service, node = self._resolve(session, slug)

        run = PipelineRun(
            kind=PipelineKind.WAKE,
            correlation_id=correlation_id,
            service_id=service.id,
            status=PipelineStatus.RUNNING,
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        audit.record(
            session,
            actor=actor,
            action="wake.start",
            target=f"service:{slug}",
            correlation_id=correlation_id,
            details={"run_id": run.id, "node": node.name},
        )
        session.commit()
        return run

    async def execute(
        self, run_id: int, slug: str, *, actor: str = "system", timeout_s: int | None = None
    ) -> None:
        """Do the actual work for a run :meth:`start` already created.

        Opens its own DB session and swallows nothing — a failure here is
        recorded on the run row and audited, never raised into a caller that
        (in the background-task case) has no one left to catch it. Callers
        that DO want the exception — the CLI, tests — should use
        :meth:`run_for_service` instead.
        """
        with session_scope() as session:
            run = session.get(PipelineRun, run_id)
            if run is None:
                log.error("wake.execute.run_missing", run_id=run_id, slug=slug)
                return
            # Already recorded on the run row and audited by _run.
            with contextlib.suppress(WakeError, AdapterError):
                await self._run(session, run, slug, actor=actor, timeout_s=timeout_s)

    async def run_for_service(
        self, session: Session, slug: str, *, actor: str = "system", timeout_s: int | None = None
    ) -> PipelineRun:
        """Start and run to completion in one call, raising on failure.

        For the CLI and tests, where blocking until the outcome is known is
        exactly what's wanted. The web layer uses :meth:`start` + :meth:`execute`
        instead so a request can return before this finishes.
        """
        run = self.start(session, slug, actor=actor)
        await self._run(session, run, slug, actor=actor, timeout_s=timeout_s)
        return run

    # -- the pipeline body ------------------------------------------------------

    async def _run(
        self,
        session: Session,
        run: PipelineRun,
        slug: str,
        *,
        actor: str,
        timeout_s: int | None,
    ) -> None:
        correlation_id = run.correlation_id
        service, node = self._resolve(session, slug)
        budget = timeout_s if timeout_s is not None else self._settings.wake_timeout_s
        deadline = time.monotonic() + budget
        steps = StepRecorder(session, run)

        hold_handle: str | None = None
        try:
            hold_handle = await self._assert_hold(session, steps, node, reason=f"wake:{slug}")
            await self._ensure_node_powered(steps, node)
            await self._wait_node_online(session, steps, node, deadline)
            await self._ensure_guest_running(steps, service, node)
            await self._wait_healthy(session, steps, service, deadline)

            self._finish(session, run, PipelineStatus.SUCCEEDED)
            audit.record(
                session,
                actor=actor,
                action="wake.succeeded",
                target=f"service:{slug}",
                correlation_id=correlation_id,
                details={"run_id": run.id},
            )
            session.commit()

        except (WakeError, AdapterError) as exc:
            # AdapterUnreachable is indeterminate, not proof the node is bad —
            # same distinction BackupPipeline draws for its gate.
            indeterminate = isinstance(exc, AdapterUnreachable)
            steps.record(
                "aborted",
                "indeterminate" if indeterminate else "failed",
                detail={"error": str(exc), "type": type(exc).__name__},
            )
            self._finish(session, run, PipelineStatus.FAILED, error=str(exc))
            audit.record(
                session,
                actor=actor,
                action="wake.failed",
                target=f"service:{slug}",
                result=AuditResult.ERROR,
                correlation_id=correlation_id,
                details={"run_id": run.id, "error": str(exc), "indeterminate": indeterminate},
            )
            session.commit()
            raise
        finally:
            if hold_handle is not None:
                await self._release_hold(session, steps, node, hold_handle)

    # -- steps ---------------------------------------------------------------

    async def _assert_hold(
        self, session: Session, steps: StepRecorder, node: Node, *, reason: str
    ) -> str:
        handle = await self._power.hold(
            node.power_mgr_target, reason=reason, ttl_s=self._settings.wake_timeout_s
        )
        state = self._node_state(session, node)
        state.hold_count += 1
        state.current_hold_reasons = [
            *state.current_hold_reasons,
            {"handle": handle, "reason": reason, "at": datetime.now(UTC).isoformat()},
        ]
        session.add(state)
        session.commit()
        steps.record("hold_asserted", "ok", detail={"handle": handle})
        return handle

    async def _release_hold(
        self, session: Session, steps: StepRecorder, node: Node, handle: str
    ) -> None:
        state = self._node_state(session, node)
        state.hold_count = max(0, state.hold_count - 1)
        state.current_hold_reasons = [
            r for r in state.current_hold_reasons if r.get("handle") != handle
        ]
        session.add(state)
        session.commit()
        try:
            await self._power.release(node.power_mgr_target, handle=handle)
            steps.record("hold_released", "ok", detail={"handle": handle})
        except AdapterError as exc:
            # Don't let a release failure mask whatever outcome is already
            # recorded — log it and move on.
            steps.record("hold_released", "failed", detail={"handle": handle, "error": str(exc)})
            log.warning("wake.hold_release_failed", node=node.name, handle=handle, error=str(exc))

    async def _ensure_node_powered(self, steps: StepRecorder, node: Node) -> None:
        try:
            cluster = await self._proxmox.cluster_status()
        except AdapterUnreachable as exc:
            # Can't tell if it's already on — a redundant "on" to a running node
            # is a harmless no-op for every power manager this targets, and
            # _wait_node_online right after this tolerates the same blip.
            steps.record(
                "node_power_check", "unreachable", detail={"node": node.name, "error": str(exc)}
            )
        else:
            if self._node_online(cluster, node.name):
                steps.record("node_power", "already_on", detail={"node": node.name})
                return
        await self._power.wake(node.power_mgr_target, reason=f"wake pipeline for {node.name}")
        steps.record(
            "node_power_on",
            "ok",
            detail={"node": node.name, "power_mgr_target": node.power_mgr_target},
        )

    async def _wait_node_online(
        self, session: Session, steps: StepRecorder, node: Node, deadline: float
    ) -> None:
        started = datetime.now(UTC)
        last_error: str | None = None
        while True:
            try:
                cluster = await self._proxmox.cluster_status()
                if self._node_online(cluster, node.name):
                    steps.record(
                        "node_online", "ok", started_at=started, detail={"node": node.name}
                    )
                    state = self._node_state(session, node)
                    state.power_state = PowerState.ON
                    state.power_state_at = datetime.now(UTC)
                    state.last_seen_at = state.power_state_at
                    session.add(state)
                    session.commit()
                    return
            except AdapterUnreachable as exc:
                # Transient — Proxmox itself may be mid-restart. Keep polling
                # rather than aborting on the first blip.
                last_error = str(exc)

            if time.monotonic() >= deadline:
                detail = f": {last_error}" if last_error else ""
                raise WakeError(
                    f"node {node.name!r} did not come online within the wake timeout{detail}"
                )
            await asyncio.sleep(self._settings.wake_poll_interval_s)

    async def _ensure_guest_running(
        self, steps: StepRecorder, service: Service, node: Node
    ) -> None:
        if service.guest_kind is GuestKind.NONE or service.guest_id is None:
            steps.record("guest_start", "skipped", detail={"reason": "service has no guest"})
            return

        guests = await self._proxmox.list_guests(node.name)
        guest = next((g for g in guests if g.vmid == service.guest_id), None)
        if guest is not None and guest.status == "running":
            steps.record("guest_start", "already_running", detail={"vmid": service.guest_id})
            return

        started = datetime.now(UTC)
        handle = await self._proxmox.start_guest(node.name, service.guest_id, service.guest_kind)
        status = await self._proxmox.wait_for_task(
            node.name, handle.upid, timeout_s=self._settings.wake_guest_start_timeout_s
        )
        if not status.succeeded:
            raise WakeError(
                f"starting guest {service.guest_id} on {node.name!r} failed: "
                f"{status.exit_status or 'unknown'}. If the cluster is non-quorate "
                f"(other nodes off), Proxmox refuses guest starts — see "
                f"docs/proxmox_connectivity.md."
            )
        steps.record(
            "guest_start",
            "ok",
            started_at=started,
            detail={"vmid": service.guest_id, "upid": handle.upid},
        )

    async def _wait_healthy(
        self, session: Session, steps: StepRecorder, service: Service, deadline: float
    ) -> None:
        started = datetime.now(UTC)
        while True:
            verdict = await self._health.scan(session, service)
            if verdict.state is HealthState.HEALTHY:
                steps.record(
                    "health_poll", "ok", started_at=started, detail={"reason": verdict.reason}
                )
                return
            if time.monotonic() >= deadline:
                raise WakeError(
                    f"{service.slug!r} did not become HEALTHY within the wake timeout — "
                    f"last verdict: {verdict.state.value} ({verdict.reason})"
                )
            await asyncio.sleep(self._settings.wake_poll_interval_s)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _node_online(cluster: ClusterStatus, name: str) -> bool:
        node = next((n for n in cluster.nodes if n.name == name), None)
        return bool(node and node.online)

    @staticmethod
    def _node_state(session: Session, node: Node) -> NodeState:
        state = session.get(NodeState, node.id)
        if state is None:
            state = NodeState(node_id=node.id)
            session.add(state)
            session.commit()
            session.refresh(state)
        return state

    def _resolve(self, session: Session, slug: str) -> tuple[Service, Node]:
        service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        if service is None:
            raise WakeError(f"no service registered with slug {slug!r}")
        if not service.enabled:
            raise WakeError(f"service {slug!r} is disabled in the registry")

        node = session.get(Node, service.node_id) if service.node_id else None
        if node is None:
            raise WakeError(f"service {slug!r} is not attached to a node")
        return service, node

    @staticmethod
    def _finish(
        session: Session, run: PipelineRun, status: PipelineStatus, *, error: str | None = None
    ) -> None:
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.error = error
        session.add(run)
        session.commit()
