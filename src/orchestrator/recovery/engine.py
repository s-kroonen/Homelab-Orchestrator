"""Limited automatic recovery.

After every health scan, for each service that opted in (a ``recovery:`` block) and
is FAILED, try the next step of its ladder - restart it through its controller, then
its parent, optionally its whole guest, optionally propose a restore - at most
``max_attempts`` times per ``window_s``, with ``settle_s`` between attempts. When the
ladder runs out it stops and says so. Rules that keep this safe:

* a service must be FAILED on ``recovery_confirm_scans`` consecutive scans first;
* a child is left alone while any ancestor is not healthy (the parent's ladder, or a
  human, deals with the root cause first);
* nothing is touched while the root is being backed up;
* DOWN (stopped / node off) and UNKNOWN are not failures - wakes handle DOWN;
* a restore is only ever PROPOSED: a person approves it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.proxmox.base import ProxmoxAdapter
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.controllers import ControllerError, build_controller
from orchestrator.db.models import PipelineRun, RecoveryState, Service
from orchestrator.domain.enums import AuditResult, HealthState, PipelineKind, PipelineStatus
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.logging_setup import get_logger
from orchestrator.notify.engine import Event, NotificationEngine
from orchestrator.pipelines.restore import RestoreError, approval_url, create_proposal
from orchestrator.recovery.policy import effective_max, is_enabled, normalize_policy
from orchestrator.registry.tree import TreeIndex

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class RecoveryEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        proxmox: ProxmoxAdapter,
        notifications: NotificationEngine | None,
    ) -> None:
        self._settings = settings
        self._proxmox = proxmox
        self._notifications = notifications

    # -- one cycle ----------------------------------------------------------------

    async def run_cycle(
        self,
        session: Session,
        verdicts: Mapping[str, ServiceVerdict],
        tree: TreeIndex,
    ) -> list[dict[str, Any]]:
        """Look at this scan's verdicts; do what the policies say. Returns what it
        did (for tests and logs). Never raises."""
        if not self._settings.recovery_enabled:
            return []
        done: list[dict[str, Any]] = []
        # Roots first, so a child sees whether its parent is being dealt with.
        for svc, _depth in tree.walk():
            policy_raw = svc.recovery or {}
            if not is_enabled(policy_raw) or svc.id is None:
                continue
            verdict = verdicts.get(svc.slug)
            if verdict is None or not tree.is_served(svc):
                continue
            try:
                policy = normalize_policy(policy_raw)
                result = await self._one(session, svc, verdict, policy, verdicts, tree)
            except Exception as exc:  # pragma: no cover - defensive
                log.error("recovery.crashed", service=svc.slug, error=str(exc))
                continue
            if result:
                done.append(result)
        session.commit()
        return done

    async def _one(
        self,
        session: Session,
        svc: Service,
        verdict: ServiceVerdict,
        policy: dict[str, Any],
        verdicts: Mapping[str, ServiceVerdict],
        tree: TreeIndex,
    ) -> dict[str, Any] | None:
        assert svc.id is not None
        state = session.get(RecoveryState, svc.id)
        if state is None:
            state = RecoveryState(service_id=svc.id)
            session.add(state)
        now = _now()
        state.updated_at = now

        if verdict.state is HealthState.HEALTHY:
            came_back = state.attempts > 0
            action = state.last_action
            state.failing_scans, state.attempts, state.exhausted = 0, 0, False
            state.window_started_at = None
            session.add(state)
            if came_back:
                await self._tell(
                    session,
                    svc,
                    "recovery_succeeded",
                    "info",
                    f"{svc.name} is healthy again",
                    f"Recovery worked ({action or 'a step'}); {svc.name} is healthy.",
                )
                return {"service": svc.slug, "event": "recovered"}
            return None

        if verdict.state is not HealthState.FAILED:
            return None  # DOWN / UNKNOWN are not ours to fix

        state.failing_scans += 1
        session.add(state)
        if state.failing_scans < max(1, self._settings.recovery_confirm_scans):
            return None
        if any(
            (verdicts.get(a.slug) is not None and verdicts[a.slug].state is not HealthState.HEALTHY)
            for a in tree.ancestors(svc)
        ):
            return None  # the root cause sits above; deal with that first

        window = timedelta(seconds=int(policy["window_s"]))
        started = _aware(state.window_started_at)
        if started is None or now - started > window:
            state.window_started_at, state.attempts, state.exhausted = now, 0, False
        if state.exhausted:
            return None
        last = _aware(state.last_attempt_at)
        if last is not None and now - last < timedelta(seconds=int(policy["settle_s"])):
            return None
        if self._backup_running(session, tree.root(svc)):
            return None

        ladder = self._applicable(session, svc, policy, tree)
        allowed = effective_max(policy, len(ladder))
        if state.attempts >= allowed:
            state.exhausted = True
            session.add(state)
            await self._tell(
                session,
                svc,
                "recovery_exhausted",
                "error",
                f"Recovery gave up on {svc.name}",
                f"{svc.name} is still FAILED after {state.attempts} automatic attempt(s) "
                f"(last: {state.last_action or 'none'} - {state.last_result or 'n/a'}). "
                "It will not be touched again until it is healthy or the window passes. "
                f"{verdict.reason}",
            )
            return {"service": svc.slug, "event": "exhausted"}

        step = ladder[state.attempts]
        state.attempts += 1
        state.last_attempt_at, state.last_action = now, step
        session.add(state)
        await self._tell(
            session,
            svc,
            "recovery_attempt",
            "info",
            f"Recovering {svc.name}: {step} (attempt {state.attempts} of {allowed})",
            f"{svc.name} is FAILED: {verdict.reason}",
        )
        ok, message = await self._do(session, svc, step, tree, verdict)
        state.last_result = ("ok: " if ok else "failed: ") + message
        session.add(state)
        audit.record(
            session,
            actor="recovery",
            action=f"recovery.{step}",
            target=f"service:{svc.slug}",
            result=AuditResult.OK if ok else AuditResult.ERROR,
            details={"attempt": state.attempts, "message": message},
        )
        return {"service": svc.slug, "event": "attempt", "step": step, "ok": ok, "message": message}

    # -- the ladder -----------------------------------------------------------------

    def _applicable(
        self, session: Session, svc: Service, policy: dict[str, Any], tree: TreeIndex
    ) -> list[str]:
        """The steps that can actually be done for this service, in order."""
        out: list[str] = []
        for step in policy["steps"]:
            if step == "restart" and not svc.controller:
                continue
            if step == "restart_parent" and self._parent_with_controller(tree, svc) is None:
                continue
            if step == "restart_guest" and not tree.effective_guest(svc).has_guest:
                continue
            out.append(step)
        return out

    @staticmethod
    def _parent_with_controller(tree: TreeIndex, svc: Service) -> Service | None:
        return next((a for a in tree.ancestors(svc) if a.controller), None)

    async def _do(
        self, session: Session, svc: Service, step: str, tree: TreeIndex, verdict: ServiceVerdict
    ) -> tuple[bool, str]:
        try:
            if step == "propose_restore":
                return await self._propose_restore(session, svc, tree, verdict)
            target = svc
            kind = "service"
            if step == "restart_parent":
                parent = self._parent_with_controller(tree, svc)
                if parent is None:
                    return False, "no parent has a controller"
                target = parent
            elif step == "restart_guest":
                kind = "guest"
            ctl = build_controller(self._settings, self._proxmox, session, target, target=kind)
            if ctl is None:
                return False, "nothing to act on"
            return True, await ctl.restart()
        except (ControllerError, ValueError, RestoreError) as exc:
            return False, str(exc)

    async def _propose_restore(
        self, session: Session, svc: Service, tree: TreeIndex, verdict: ServiceVerdict
    ) -> tuple[bool, str]:
        root = tree.root(svc)
        approval = create_proposal(
            session,
            self._settings,
            root,
            requested_by="recovery",
            reason=f"{svc.slug} stayed FAILED after automatic recovery: {verdict.reason}"[:400],
        )
        session.flush()
        url = approval_url(self._settings, approval.token)
        await self._tell(
            session,
            root,
            "restore_proposed",
            "error",
            f"Restore proposed for {root.name}",
            f"{svc.name} is still FAILED. Restoring {approval.payload['snapshot']} would "
            "replace the whole guest with that copy. Nothing happens until you approve."
            + ("" if url else f" Open /approvals/{approval.token} on the dashboard."),
            approve_url=url,
        )
        return True, f"restore of {root.slug!r} proposed ({approval.payload['snapshot']})"

    # -- helpers --------------------------------------------------------------------

    @staticmethod
    def _backup_running(session: Session, root: Service) -> bool:
        return (
            session.exec(
                select(PipelineRun.id)
                .where(
                    PipelineRun.service_id == root.id,
                    PipelineRun.kind == PipelineKind.BACKUP,
                    PipelineRun.status == PipelineStatus.RUNNING,
                )
                .limit(1)
            ).first()
            is not None
        )

    async def _tell(
        self,
        session: Session,
        svc: Service,
        event: str,
        severity: str,
        title: str,
        body: str,
        *,
        approve_url: str | None = None,
    ) -> None:
        if self._notifications is None:
            return
        await self._notifications.emit(
            session,
            Event(
                level="sub" if svc.parent_id is not None else "service",
                key=svc.slug,
                name=svc.name,
                event=event,
                severity=severity,
                title=title,
                body=body[:1500],
                scope={"slug": svc.slug, "group": svc.group},
                tags=["recovery"],
                approve_url=approve_url,
            ),
        )
