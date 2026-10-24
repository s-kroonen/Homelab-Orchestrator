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
from dataclasses import dataclass
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
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.steps import Recorder, StepRecorder
from orchestrator.registry.tree import effective_guest, is_served

log = get_logger(__name__)


class WakeError(Exception):
    """Raised when a wake cannot proceed or did not complete in time."""


class WakeUnsupportedError(WakeError):
    """Raised when a wake is structurally IMPOSSIBLE for this service/node —
    not merely failed, timed out, or indeterminate.

    A subclass of :class:`WakeError` (not a sibling) so every existing
    ``except WakeError`` call site (``_run`` below, ``maintenance.py``,
    ``web/routers/wake.py``, the CLI) keeps catching it with no code change
    — it is still "a wake error". Callers that want to tell this ONE case
    apart from a real failure (retrying would help) or a timeout (retrying
    MIGHT help) catch :class:`WakeUnsupportedError` specifically, and catch
    it *before* the broader ``WakeError`` where both appear (see
    ``maintenance.py``).

    Raised as early as possible — see :func:`wake_is_unsupported` and
    :meth:`WakePipeline._resolve` — before any hold is asserted, any power
    command sent, or any polling begins. Retrying changes nothing about this
    case, unlike a timeout or a transient adapter failure.

    Deliberately does NOT cover an unimplemented/dry-run power adapter
    (``PowerAdapter.IMPLEMENTED is False``). That is a different kind of
    "not ready" — a deliberate, operator-visible deployment-time choice
    (DRY_RUN=true, or a power adapter not yet wired up) the operator already
    knows about from their own `.env`, not a per-node data gap a visitor
    might be the first to discover. Mixing the two would also make this
    exception fire for every service in a dry-run/staging deployment, which
    is the normal, expected state there, not an error worth a distinct
    "contact an administrator" page.
    """


def wake_is_unsupported(node: Node | None) -> bool:
    """True when a wake for this node is structurally impossible: no node
    at all, or no way to command it on (``power_mgr_target`` blank and not
    ``always_on``).

    Shared by :meth:`WakePipeline._resolve` (to raise
    :class:`WakeUnsupportedError` as early as possible) and
    ``web/routers/maintenance.py``/``web/routers/dashboard.py`` (to show the
    same signal up front — on the maintenance page before even attempting a
    wake, and on the service detail page before anything is down at all —
    without needing to parse a stored error string after the fact). Kept as
    one function so the three call sites can never drift out of sync on
    what "unsupported" means.

    Deliberately does NOT check whether the node happens to already be
    online right now (that would need an async Proxmox call, and this is
    used from synchronous contexts). A node that's already on but has no
    `power_mgr_target` set is a real, if unusual, data gap — this still
    flags it, trading a rare false positive (it would have "woken"
    trivially via `_wait_node_online` finding it already up) for a cheap,
    instant, always-synchronous check everywhere this is called.
    """
    if node is None:
        return True
    return not node.power_mgr_target and not node.always_on


@dataclass(frozen=True)
class PriorState:
    """What a service's node and guest were doing before anything was woken."""

    node_id: int
    node_name: str
    node_online: bool
    guest_vmid: int | None
    guest_kind: GuestKind
    #: None when there is no guest or it could not be read.
    guest_running: bool | None


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
        self._health = health or HealthEngine(settings=settings, proxmox=proxmox)

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

    async def ensure_awake(
        self,
        session: Session,
        slug: str,
        *,
        actor: str = "system",
        timeout_s: int | None = None,
        own_runs: set[int] | None = None,
    ) -> PipelineRun:
        """Wake ``slug`` and return only once it is up — or raise.

        ``own_runs``: filled with the id of the wake run THIS call started (not one
        it merely joined), even if that run then fails - so a backup can tell its
        own wakes from somebody else's.

        What the backup pipeline calls for every prerequisite and target. It
        goes through :meth:`get_or_start`, so a backup that needs a node a
        visitor (or another backup) is already waking JOINS that wake instead
        of firing a second one, and a wake that just finished inside the
        cooldown is reused rather than repeated.

        * a new run is executed inline on ``session`` via :meth:`_run`, which
          records its own steps and audit rows and raises
          :class:`WakeError` / :class:`WakeUnsupportedError` / ``AdapterError``;
        * a run owned by someone else is polled until it leaves RUNNING (bounded
          by the wake budget), then judged by its final status.
        """
        run, started_new = self.get_or_start(session, slug, actor=actor)
        if started_new and own_runs is not None and run.id is not None:
            own_runs.add(run.id)
        if started_new:
            await self._run(session, run, slug, actor=actor, timeout_s=timeout_s)
            return run

        budget = timeout_s if timeout_s is not None else self._settings.wake_timeout_s
        deadline = time.monotonic() + budget
        while run.status is PipelineStatus.RUNNING:
            if time.monotonic() >= deadline:
                raise WakeError(
                    f"a wake for {slug!r} (run #{run.id}) started elsewhere is still "
                    f"running after {budget}s"
                )
            # Close the read transaction before sleeping: nothing here may sit
            # on a DB lock while a node takes minutes to boot.
            session.commit()
            await asyncio.sleep(self._settings.wake_poll_interval_s)
            session.refresh(run)
        if run.status is PipelineStatus.SUCCEEDED:
            return run
        raise WakeError(
            f"the latest wake for {slug!r} (run #{run.id}) {run.status.value}: "
            f"{run.error or 'no error recorded'}"
        )

    async def is_up(self, session: Session, slug: str) -> bool:
        """Cheap "does this need waking at all?": node online in the cluster and
        its guest (if any) running. Never raises and never starts anything — if
        Proxmox cannot be asked, the answer is "not known to be up", which sends
        the caller down the full wake path (that path tolerates the same blip).
        """
        service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        node = session.get(Node, service.node_id) if service and service.node_id else None
        if service is None or node is None:
            return False
        try:
            cluster = await self._proxmox.cluster_status()
            if not self._node_online(cluster, node.name):
                return False
            eg = effective_guest(session, service)
            if eg.kind is GuestKind.NONE or eg.guest_id is None:
                return True
            guests = await self._proxmox.list_guests(node.name)
        except AdapterError:
            return False
        guest = next((g for g in guests if g.vmid == eg.guest_id), None)
        return bool(guest and guest.status == "running")

    async def prior_state(self, session: Session, slug: str) -> PriorState | None:
        """What is up RIGHT NOW for ``slug``'s node and guest - read before a backup
        wakes anything, so it can later put things back. ``None`` when Proxmox
        cannot be asked (then nothing is recorded and nothing is turned off)."""
        service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        node = session.get(Node, service.node_id) if service and service.node_id else None
        if service is None or node is None or node.id is None:
            return None
        try:
            cluster = await self._proxmox.cluster_status()
            node_online = self._node_online(cluster, node.name)
            eg = effective_guest(session, service)
            running: bool | None = None
            if node_online and eg.kind is not GuestKind.NONE and eg.guest_id is not None:
                guests = await self._proxmox.list_guests(node.name)
                guest = next((g for g in guests if g.vmid == eg.guest_id), None)
                running = bool(guest and guest.status == "running")
            elif eg.kind is not GuestKind.NONE and eg.guest_id is not None:
                running = False  # the node is off, so the guest is
        except AdapterError:
            return None
        return PriorState(
            node_id=node.id,
            node_name=node.name,
            node_online=node_online,
            guest_vmid=eg.guest_id if eg.kind is not GuestKind.NONE else None,
            guest_kind=eg.kind,
            guest_running=running,
        )

    async def stop_guest(self, node: str, vmid: int, kind: GuestKind) -> str:
        """Gracefully stop a guest and wait for it. Returns the task status text."""
        handle = await self._proxmox.stop_guest(node, vmid, kind)
        status = await self._proxmox.wait_for_task(
            node, handle.upid, timeout_s=self._settings.wake_guest_start_timeout_s
        )
        if not status.succeeded:
            raise WakeError(
                f"stopping {kind.value} {vmid} on {node!r} failed: {status.exit_status}"
            )
        return handle.upid

    async def power_off_node(self, node: Node, *, reason: str) -> None:
        """Gracefully power a node off through the power manager."""
        await self._power.power_off(node.power_mgr_target, reason=reason, force=False)

    @staticmethod
    def wakes_since(session: Session, node: Node, since: datetime, *, exclude: set[int]) -> int:
        """Wake runs for services on ``node`` that someone ELSE started after
        ``since`` - a visitor or another backup who now needs it."""
        runs = session.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.WAKE)).all()
        count = 0
        for run in runs:
            if run.id in exclude or run.service_id is None:
                continue
            started = (
                run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=UTC)
            )
            if started < since:
                continue
            svc = session.get(Service, run.service_id)
            if svc is not None and svc.node_id == node.id:
                count += 1
        return count

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
            hold_handle = await self.assert_hold(session, steps, node, reason=f"wake:{slug}")
            await self._ensure_node_powered(steps, node)
            await self._wait_node_online(session, steps, node, deadline)
            await self._ensure_guest_running(session, steps, service, node)
            await self._ensure_service_running(session, steps, service)
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
            await announce(
                session,
                level="service",
                key=slug,
                name=service.name,
                event="wake_failed",
                severity="warn",
                title=f"Waking {service.name} failed",
                body=str(exc)[:1500],
                scope={"slug": slug, "group": service.group},
            )
            raise
        finally:
            if hold_handle is not None:
                await self.release_hold(session, steps, node, hold_handle)

    # -- steps ---------------------------------------------------------------

    async def assert_hold(
        self,
        session: Session,
        steps: Recorder,
        node: Node,
        *,
        reason: str,
        ttl_s: int | None = None,
    ) -> str:
        """Assert an inhibit-shutdown hold on ``node`` and account for it in
        ``NodeState``. Public because the backup pipeline holds the nodes it
        woke for the whole backup — a hold must outlive the wake that asked for
        it, so the wake's own (short, ``wake_timeout_s``) hold is not enough.
        ``ttl_s`` defaults to the wake timeout."""
        handle = await self._power.hold(
            node.power_mgr_target,
            reason=reason,
            ttl_s=ttl_s if ttl_s is not None else self._settings.wake_timeout_s,
        )
        state = self._node_state(session, node)
        state.hold_count += 1
        state.current_hold_reasons = [
            *state.current_hold_reasons,
            {"handle": handle, "reason": reason, "at": datetime.now(UTC).isoformat()},
        ]
        session.add(state)
        session.commit()
        steps.record(
            "hold_asserted", "ok", detail={"handle": handle, "node": node.name, "reason": reason}
        )
        return handle

    async def release_hold(
        self, session: Session, steps: Recorder, node: Node, handle: str
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
            steps.record("hold_released", "ok", detail={"handle": handle, "node": node.name})
        except AdapterError as exc:
            # Don't let a release failure mask whatever outcome is already
            # recorded — log it and move on.
            steps.record(
                "hold_released",
                "failed",
                detail={"handle": handle, "node": node.name, "error": str(exc)},
            )
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
        self, session: Session, steps: StepRecorder, service: Service, node: Node
    ) -> None:
        # A sub-service wakes its root's guest.
        eg = effective_guest(session, service)
        if eg.kind is GuestKind.NONE or eg.guest_id is None:
            steps.record("guest_start", "skipped", detail={"reason": "service has no guest"})
            return

        guests = await self._proxmox.list_guests(node.name)
        guest = next((g for g in guests if g.vmid == eg.guest_id), None)
        if guest is not None and guest.status == "running":
            steps.record("guest_start", "already_running", detail={"vmid": eg.guest_id})
            return

        started = datetime.now(UTC)
        handle = await self._proxmox.start_guest(node.name, eg.guest_id, eg.kind)
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

    async def _ensure_service_running(
        self, session: Session, steps: StepRecorder, service: Service
    ) -> None:
        """A service with its own controller (a container, an add-on) is started
        through it once its guest is up. A root with none is up when its guest is."""
        if not service.controller:
            return
        from orchestrator.controllers import ControllerError, build_controller

        started = datetime.now(UTC)
        try:
            ctl = build_controller(self._settings, self._proxmox, session, service)
            if ctl is None:
                return
            status = await ctl.status()
            if status.running:
                steps.record(
                    "service_start", "already_running", detail={"controller": ctl.describe()}
                )
                return
            message = await ctl.start()
        except (ControllerError, ValueError) as exc:
            raise WakeError(
                f"starting {service.slug!r} through its controller failed: {exc}"
            ) from exc
        steps.record(
            "service_start",
            "ok",
            started_at=started,
            detail={"controller": ctl.describe(), "message": message},
        )

    async def _wait_healthy(
        self, session: Session, steps: StepRecorder, service: Service, deadline: float
    ) -> None:
        started = datetime.now(UTC)
        while True:
            # Every non-HEALTHY verdict means "not yet" here — DOWN included:
            # right after start_guest the guest may still be booting, and each
            # scan re-reads its state fresh from Proxmox.
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
        if not is_served(session, service):
            raise WakeError(f"service {slug!r} (or a service above it) is disabled in the registry")

        node = session.get(Node, service.node_id) if service.node_id else None
        if node is None:
            raise WakeError(f"service {slug!r} is not attached to a node")
        if wake_is_unsupported(node):
            raise WakeUnsupportedError(
                f"node {node.name!r} has no power management configured — "
                f"power_mgr_target is blank and it is not marked always_on, so there is "
                f"no way for the orchestrator to turn it on. An administrator must set "
                f"one of those on the node before {slug!r} can be auto-started."
            )
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
