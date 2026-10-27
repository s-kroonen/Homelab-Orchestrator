"""Notice deploys that Portainer did by itself (its own Git webhook, auto-update, a click in
its UI) and run the *post* checks on them.

Pre-guards cannot run after the fact, so an outside deploy is recorded as a deploy run that
starts at "it happened": it waits for the stack, checks the probes and, if they fail, does
what the application's ``on_failure`` says - the same as for a deploy started here. The only
thing watched is the Git commit the stack is on (or, for a stack not from Git, its update
time); stopping and starting a stack changes neither, so a switch is never mistaken for a deploy.
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.portainer.base import Stack
from orchestrator.config import Settings
from orchestrator.db.models import PipelineRun, Service
from orchestrator.deploy.pipeline import DeployPipeline, DeployRefused, Trigger
from orchestrator.domain.enums import PipelineKind, PipelineStatus
from orchestrator.logging_setup import get_logger
from orchestrator.notify.hooks import announce

log = get_logger(__name__)

#: slug -> what the stack was on when last looked at
_BASELINE: dict[str, str] = {}


def _marker(stack: Stack) -> str:
    if stack.git and stack.git.hash:
        return f"git:{stack.git.hash}"
    return f"updated:{stack.updated_at or ''}"


def remember(slug: str, stack: Stack) -> None:
    """A deploy started here moved the stack; do not report it as an outside one."""
    _BASELINE[slug] = _marker(stack)


def forget_all() -> None:
    _BASELINE.clear()


class DeployWatcher:
    async def observe(self, session: Session, adapters: AdapterBundle, settings: Settings) -> int:
        """One pass over the services with a deploy method. Returns how many outside deploys
        were started to be checked."""
        if not any(a.configured for a in adapters.portainers.values()):
            return 0
        started = 0
        down: set[str] = set()  # Portainers that stopped answering during this pass
        pipeline = DeployPipeline(adapters, settings)
        running = {
            r.service_id
            for r in session.exec(
                select(PipelineRun).where(
                    col(PipelineRun.kind).in_([PipelineKind.DEPLOY, PipelineKind.SWITCH]),
                    PipelineRun.status == PipelineStatus.RUNNING,
                )
            ).all()
        }
        for svc in session.exec(select(Service).order_by(Service.slug)).all():
            cfg = svc.controller or {}
            if not svc.deploy or cfg.get("type") != "portainer" or not cfg.get("stack"):
                continue
            named = str(cfg.get("instance") or "")
            adapter = adapters.portainer_for(named)
            if adapter is None or not adapter.configured or adapter.instance_id in down:
                continue
            try:
                stack = await adapter.get_stack(int(cfg["endpoint"]), str(cfg["stack"]))
            except AdapterUnreachable:
                down.add(adapter.instance_id)  # not answering: its other stacks would only wait
                continue
            except AdapterError:
                continue
            if stack is None:
                continue
            now, before = _marker(stack), _BASELINE.get(svc.slug)
            _BASELINE[svc.slug] = now
            if before is None or before == now or svc.id in running:
                continue
            if not svc.deploy.get("enabled", True):
                continue
            trigger = Trigger(
                "outside",
                "portainer",
                {"from": before.split(":", 1)[-1][:12], "to": now.split(":", 1)[-1][:12]},
            )
            try:
                run = pipeline.start(session, svc.slug, trigger)
            except DeployRefused as exc:
                log.info("deploy.outside_not_checked", slug=svc.slug, reason=str(exc))
                continue
            started += 1
            await announce(
                session,
                level="sub" if svc.parent_id else "service",
                key=svc.slug,
                name=svc.name or svc.slug,
                event="deploy_outside",
                severity="info",
                title=f"{svc.slug} was deployed from outside",
                body="Portainer redeployed it on its own; the health checks are running.",
                scope={"slug": svc.slug},
            )
            assert run.id is not None
            pipeline.fire(run.id, svc.slug, trigger, outside=True)
        return started


__all__ = ["DeployWatcher", "forget_all", "remember"]
