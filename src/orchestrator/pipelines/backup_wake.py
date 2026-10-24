"""The "wake prerequisites" phase of a backup.

A backup used to check PBS and dump; nothing woke anything, so a burst node that
was off just failed the run. This module is the new first phase: walk the
resolved plan (:func:`orchestrator.registry.backup_plan.resolve_backup_plan`),
and for each service

1. assert a power-manager HOLD on its node, so the manager's idle shutdown cannot
   pull the node (TrueNAS!) mid-dump, then
2. wake it — unless it is already up (node online, guest running), in which case
   nothing is fired.

The wake itself is :meth:`WakePipeline.ensure_awake`, i.e. the existing wake
pipeline with its ``get_or_start`` de-duplication and cooldown: a backup needing
a node a visitor is already waking joins that wake instead of double-firing.

Holds are NOT released here. They outlive the wake and belong to the whole
backup, so :class:`WakeContext` carries them back to the caller, which releases
them in a ``finally`` (:meth:`BackupWaker.release`) — success, failure or
cancellation.

A scheduled policy backs up several services behind the same prerequisites.
It builds one :class:`WakeContext` for those (``BackupPipeline.prepare_policy``),
holds it for the whole batch, and hands it to every per-service run as
``shared``: those runs replay the shared outcome as a step instead of waking
again.

Nothing is recorded when there is nothing to do — a service with no
prerequisites whose node is already up behaves exactly as before this phase
existed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable
from orchestrator.config import Settings
from orchestrator.db.models import Node, NodeState, Service
from orchestrator.domain.enums import GuestKind
from orchestrator.logging_setup import get_logger
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.steps import Recorder, RetryableBackupError
from orchestrator.pipelines.wake import WakeError, WakePipeline, WakeUnsupportedError
from orchestrator.registry.backup_plan import BLOCKER_NO_POWER_TARGET, PlanStep

log = get_logger(__name__)

NO_POWER_TARGET_MESSAGE = (
    "cannot be woken: no power target configured "
    "(set the node's power target or mark it always on)"
)
_UNTOUCHED = "No backup was taken and the last known-good backup is untouched."


class PrerequisiteError(RetryableBackupError):
    """A prerequisite or the backup target could not be brought up. The backup
    run fails at this step; the gate and the dump never run."""


class _LogSteps:
    """Step sink for a batch with no ``PipelineRun`` of its own."""

    def record(
        self,
        name: str,
        status: str,
        *,
        detail: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> None:
        log.info("backup.wake.step", step=name, status=status, **(detail or {}))


@dataclass
class HeldNode:
    node_id: int
    node_name: str
    handle: str


@dataclass
class NodePrior:
    """What a node was doing before this backup touched it - what to put back."""

    node_id: int
    node_name: str
    #: The node was already on. If False, the backup powered it on and, when done,
    #: powers it off again (graceful); if True only the guests it started are stopped.
    was_online: bool
    started_guests: list[tuple[int, GuestKind]] = field(default_factory=list)


@dataclass
class WakeContext:
    """What a wake phase leaves behind: the holds still to release, and the
    per-service outcome so a later run can replay it."""

    held: list[HeldNode] = field(default_factory=list)
    #: slug -> (status, detail). Status is "ok" or "failed".
    results: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    error: str | None = None
    failed_slug: str | None = None
    #: node name -> what it was before we woke anything on it.
    prior: dict[str, NodePrior] = field(default_factory=dict)
    #: ids of the wake runs this context started (so "someone else woke it" can be told apart).
    wake_run_ids: set[int] = field(default_factory=set)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def held_node_ids(self) -> set[int]:
        return {h.node_id for h in self.held}


def step_name(entry: PlanStep) -> str:
    return f"{'wake_target' if entry.is_target else 'wake_prerequisite'}:{entry.slug}"


def _label(entry: PlanStep) -> str:
    return "backup target" if entry.is_target else "prerequisite"


class BackupWaker:
    def __init__(self, wake: WakePipeline, settings: Settings) -> None:
        self._wake = wake
        self._settings = settings

    def hold_ttl_s(self, services: int = 1) -> int:
        """Upper bound on how long a hold may be needed. A crashed orchestrator
        cannot pin a node forever, but a healthy long dump must not lose it."""
        s = self._settings
        return s.wake_timeout_s * 2 + max(1, services) * (
            s.backup_task_timeout_s + s.verify_task_timeout_s
        )

    async def run(
        self,
        session: Session,
        entries: list[PlanStep],
        ctx: WakeContext,
        steps: Recorder | None,
        *,
        actor: str,
        hold_reason: str,
        ttl_s: int,
        shared: WakeContext | None = None,
    ) -> None:
        """Bring every entry up, in order, filling ``ctx`` as it goes.

        Raises :class:`PrerequisiteError` at the first entry that cannot be
        brought up, after recording a failed step for it. ``ctx`` is filled even
        on failure (holds taken so far, the failing result), so the caller can
        always release.
        """
        sink: Recorder = steps or _LogSteps()
        shared_results = shared.results if shared else {}

        if shared is not None and shared.error:
            slug = shared.failed_slug
            entry = next((e for e in entries if e.slug == slug), None)
            if entry is not None:
                status, detail = shared.results.get(slug or "", ("failed", {"error": shared.error}))
                sink.record(step_name(entry), "failed", detail={**detail, "shared": True})
            raise PrerequisiteError(shared.error)

        pending = [e for e in entries if e.slug not in shared_results]
        up = {e.slug: await self._wake.is_up(session, e.slug) for e in pending}

        # Fail fast: a service that can never be woken must not be discovered
        # after earlier entries already powered nodes on. Only matters when it
        # actually needs waking — a blocked node that is up right now is just up.
        for entry in pending:
            if entry.blocker and not up[entry.slug]:
                self._fail(ctx, sink, entry, self._blocker_message(entry), kind="failed")

        if not shared_results and all(e.is_target and up[e.slug] for e in pending):
            return  # nothing to wake, nothing to hold, nothing worth recording

        for entry in entries:
            if entry.slug in shared_results:
                status, detail = shared_results[entry.slug]
                sink.record(step_name(entry), status, detail={**detail, "shared": True})
                continue
            await self._bring_up(
                session, entry, up[entry.slug], ctx, sink, shared, actor, hold_reason, ttl_s
            )

    async def _bring_up(
        self,
        session: Session,
        entry: PlanStep,
        already_up: bool,
        ctx: WakeContext,
        sink: Recorder,
        shared: WakeContext | None,
        actor: str,
        hold_reason: str,
        ttl_s: int,
    ) -> None:
        started = datetime.now(UTC)
        detail: dict[str, Any] = {"reason": entry.reason, "node": entry.node}
        try:
            await self._hold(session, entry, ctx, sink, shared, hold_reason, ttl_s)
            if already_up:
                detail["already_up"] = True
            else:
                await self._remember_prior(session, entry.slug, ctx)
                run = await self._wake.ensure_awake(
                    session, entry.slug, actor=actor, own_runs=ctx.wake_run_ids
                )
                detail.update(woke=True, wake_run_id=run.id)
        except WakeUnsupportedError:
            self._fail(ctx, sink, entry, self._blocker_message(entry, force=True), kind="failed")
        except (WakeError, AdapterError) as exc:
            kind = "indeterminate" if isinstance(exc, AdapterUnreachable) else "failed"
            self._fail(
                ctx,
                sink,
                entry,
                f"could not bring up {_label(entry)} {entry.slug!r} on node "
                f"{entry.node!r}: {exc}. {_UNTOUCHED}",
                kind=kind,
                detail={"type": type(exc).__name__},
            )
        ctx.results[entry.slug] = ("ok", detail)
        sink.record(step_name(entry), "ok", started_at=started, detail=detail)

    async def _hold(
        self,
        session: Session,
        entry: PlanStep,
        ctx: WakeContext,
        sink: Recorder,
        shared: WakeContext | None,
        reason: str,
        ttl_s: int,
    ) -> None:
        svc = session.exec(select(Service).where(Service.slug == entry.slug)).one()
        node = session.get(Node, svc.node_id) if svc.node_id else None
        if node is None or node.id is None or node.always_on or not node.power_mgr_target:
            return  # an always-on node is never idle-shut-down; nothing to hold
        if node.id in ctx.held_node_ids or (shared and node.id in shared.held_node_ids):
            return
        handle = await self._wake.assert_hold(session, sink, node, reason=reason, ttl_s=ttl_s)
        ctx.held.append(HeldNode(node_id=node.id, node_name=node.name, handle=handle))

    async def _remember_prior(self, session: Session, slug: str, ctx: WakeContext) -> None:
        """Note what the node and guest were doing before this wake, once per node
        (a node already recorded as 'we powered it on' stays that way)."""
        prior = await self._wake.prior_state(session, slug)
        if prior is None:
            return
        node = ctx.prior.setdefault(
            prior.node_name,
            NodePrior(prior.node_id, prior.node_name, was_online=prior.node_online),
        )
        if prior.guest_running is False and prior.guest_vmid is not None:
            item = (prior.guest_vmid, prior.guest_kind)
            if item not in node.started_guests:
                node.started_guests.append(item)

    # -- putting things back ---------------------------------------------------

    async def restore(
        self,
        session: Session,
        ctx: WakeContext,
        steps: Recorder | None,
        *,
        defer_to: WakeContext | None = None,
    ) -> None:
        """Return nodes and guests to the state they were in before the backup:
        stop the guests it started, and power off a node it powered on - unless
        something else now needs them (another hold, or a wake somebody else
        started since). Never raises; a failure is recorded, announced, and the
        node simply stays on. ``defer_to``: a scheduled batch is still running, so
        a node it powered on is handed to the batch to power off at the very end
        rather than between two services."""
        sink: Recorder = steps or _LogSteps()
        if defer_to is not None:
            # The batch's own wakes must not look like "somebody else's".
            defer_to.wake_run_ids |= ctx.wake_run_ids
        while ctx.prior:
            name, prior = ctx.prior.popitem()
            step = f"restore_state:{name}"
            node = session.get(Node, prior.node_id)
            if node is None:
                continue
            if defer_to is not None and not prior.was_online:
                defer_to.prior.setdefault(name, prior)
                sink.record(step, "deferred", detail={"reason": "the batch is still running"})
                continue
            try:
                await self._restore_node(session, ctx, sink, step, node, prior)
            except Exception as exc:
                session.rollback()
                sink.record(step, "failed", detail={"error": str(exc)})
                log.error(
                    "backup.restore_state_failed",
                    node=name,
                    error=str(exc),
                    impact="The node/guests the backup started were left running.",
                )
                try:
                    await announce(
                        session,
                        level="node",
                        key=name,
                        name=name,
                        event="power_restore_failed",
                        severity="warn",
                        title=f"{name} could not be put back after the backup",
                        body=f"{exc}\n\nThe node was turned on for a backup and is still on.",
                        scope={"node": name},
                    )
                except Exception:  # announce is best-effort
                    log.warning("backup.restore_announce_failed", node=name)

    async def _restore_node(
        self,
        session: Session,
        ctx: WakeContext,
        sink: Recorder,
        step: str,
        node: Node,
        prior: NodePrior,
    ) -> None:
        busy = self._busy_reason(session, ctx, node)
        if busy:
            sink.record(step, "skipped", detail={"reason": busy})
            return
        if prior.was_online:
            for vmid, kind in prior.started_guests:
                started = datetime.now(UTC)
                upid = await self._wake.stop_guest(node.name, vmid, kind)
                sink.record(
                    step,
                    "ok",
                    started_at=started,
                    detail={"action": "stop_guest", "vmid": vmid, "kind": kind.value, "upid": upid},
                )
            return
        if node.always_on or not node.power_mgr_target:
            sink.record(step, "skipped", detail={"reason": "node cannot be powered off here"})
            return
        grace = self._settings.backup_power_off_grace_s
        if grace > 0:
            await asyncio.sleep(grace)
            busy = self._busy_reason(session, ctx, node)
            if busy:
                sink.record(step, "skipped", detail={"reason": busy})
                return
        started = datetime.now(UTC)
        await self._wake.power_off_node(node, reason="backup finished: back to the prior state")
        sink.record(
            step,
            "ok",
            started_at=started,
            detail={"action": "power_off", "power_mgr_target": node.power_mgr_target},
        )

    def _busy_reason(self, session: Session, ctx: WakeContext, node: Node) -> str | None:
        """Why this node must be left alone, or None."""
        state = session.get(NodeState, node.id) if node.id is not None else None
        if state is not None and state.hold_count > 0:
            who = ", ".join(str(r.get("reason", "?")) for r in state.current_hold_reasons)
            return f"other work still holds it ({who or f'hold_count={state.hold_count}'})"
        if self._wake.wakes_since(session, node, ctx.started_at, exclude=ctx.wake_run_ids):
            return "something else woke a service on it during the backup"
        return None

    async def release(self, session: Session, ctx: WakeContext, steps: Recorder | None) -> None:
        """Release every hold in ``ctx`` (idempotent). Never raises: a failing
        release must not mask the outcome already recorded."""
        sink: Recorder = steps or _LogSteps()
        while ctx.held:
            held = ctx.held.pop()
            try:
                node = session.get(Node, held.node_id)
                if node is not None:
                    await self._wake.release_hold(session, sink, node, held.handle)
            except Exception as exc:
                log.error(
                    "backup.wake.release_failed",
                    node=held.node_name,
                    handle=held.handle,
                    error=str(exc),
                    impact="The hold expires on its own TTL.",
                )
                session.rollback()

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _blocker_message(entry: PlanStep, *, force: bool = False) -> str:
        who = f"{_label(entry)} {entry.slug!r} (node {entry.node!r})"
        if force or entry.blocker == BLOCKER_NO_POWER_TARGET:
            return f"{who} {NO_POWER_TARGET_MESSAGE}. {_UNTOUCHED}"
        return f"{who} cannot be woken: {entry.blocker}. {_UNTOUCHED}"

    @staticmethod
    def _fail(
        ctx: WakeContext,
        sink: Recorder,
        entry: PlanStep,
        message: str,
        *,
        kind: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        full = {"reason": entry.reason, "node": entry.node, "error": message, **(detail or {})}
        ctx.results[entry.slug] = ("failed", full)
        ctx.error = message
        ctx.failed_slug = entry.slug
        sink.record(step_name(entry), kind, detail=full)
        raise PrerequisiteError(message)
