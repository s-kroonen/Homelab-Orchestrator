"""The one on/off switch for a service and everything linked to it.

Turning a service **off** takes the outermost layer down first: its proxy hosts stop being
served, the stack or container stops, then (only when asked, and only for a root service) the
guest stops. Everything below it in the tree goes first. Turning it **on** goes the other way,
innermost first: guest, stack or container (waited for), then the proxy hosts are served.

``Service.enabled`` is the operator's intent and is written here; what is on or off is always
read back from Traefik Manager, Portainer and Proxmox afterwards. Children of a service that
comes back keep their own intent and are only *monitored* (shown as mixed if their stack is
still down) unless ``restore_children`` is set. See docs/portainer_deploys.md.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.controllers import ControllerError, build_controller
from orchestrator.db.models import PipelineRun, Service
from orchestrator.domain.enums import AuditResult, PipelineKind, PipelineStatus
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.steps import StepRecorder
from orchestrator.registry import push as push_registry
from orchestrator.registry.host_cascade import resync_subtree_probes
from orchestrator.registry.linked import (
    LinkedContext,
    LinkedState,
    Part,
    gather_context,
    portainer_cfg,
    proxy_hosts_of,
    read_linked,
)
from orchestrator.registry.tree import TreeIndex

log = get_logger(__name__)


class SwitchRefused(Exception):
    """The switch was not attempted, and why (shown to the operator)."""


@dataclass
class SwitchResult:
    slug: str
    want: bool
    ok: bool
    run_id: int | None
    steps: list[dict[str, Any]] = field(default_factory=list)
    state: LinkedState | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "want": "on" if self.want else "off",
            "ok": self.ok,
            "run_id": self.run_id,
            "message": self.message,
            "steps": self.steps,
            "state": self.state.as_dict() if self.state else None,
        }


# -- refusals -------------------------------------------------------------------------------


def busy_reason(
    session: Session, ids: list[int | None], ignore_run: int | None = None
) -> str | None:
    """Why the services must not be touched right now (a switch or a deploy is running
    for one of them), or None."""
    live = [i for i in ids if i is not None]
    if live:
        for running in session.exec(
            select(PipelineRun).where(
                col(PipelineRun.service_id).in_(live),
                col(PipelineRun.kind).in_([PipelineKind.SWITCH, PipelineKind.DEPLOY]),
                PipelineRun.status == PipelineStatus.RUNNING,
            )
        ).all():
            if running.id != ignore_run:
                return (
                    f"a {running.kind.value} is already running for this service "
                    f"(run {running.id})"
                )
    return None


def _check_allowed(
    session: Session, svc: Service, tree: TreeIndex, want: bool, ignore_run: int | None
) -> None:
    from orchestrator.web.routers.dashboard import _backup_currently_running

    subtree = [svc, *tree.descendants(svc)]
    busy = busy_reason(session, [s.id for s in subtree], ignore_run)
    if busy:
        raise SwitchRefused(busy)
    root = tree.root(svc)
    if not want and root.id is not None and _backup_currently_running(session, root.id):
        raise SwitchRefused(f"a backup of {root.slug!r} is running - not switching anything off")
    if want:
        off = [a.slug for a in tree.ancestors(svc) if not a.enabled]
        if off:
            raise SwitchRefused(f"switch {off[0]!r} on first: {svc.slug!r} is under it")


# -- acting on one linked thing ---------------------------------------------------------------


async def _portainer_do(adapters: AdapterBundle, cfg: dict[str, Any], verb: str) -> str:
    adapter = adapters.portainer_for(str(cfg.get("instance") or ""))
    if adapter is None:
        raise ControllerError(f"there is no Portainer connection called {cfg.get('instance')!r}")
    if not adapter.configured:
        raise ControllerError("Portainer is not set up")
    eid = int(cfg["endpoint"])
    try:
        if cfg.get("stack"):
            stack = await adapter.get_stack(eid, str(cfg["stack"]))
            if stack is None:
                raise ControllerError(f"stack {cfg['stack']!r} was not found in Portainer")
            if verb == "start":
                await adapter.start_stack(stack)
            else:
                await adapter.stop_stack(stack)
            return f"{verb} sent to stack {stack.name}"
        return await adapter.container_action(eid, str(cfg["container"]), verb)
    except AdapterError as exc:
        raise ControllerError(str(exc)) from exc


async def _do(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    svc: Service,
    part: Part,
    verb: str,
) -> str:
    if part.kind in {"stack", "container"}:
        cfg = portainer_cfg(svc)
        assert cfg is not None  # the part came from it
        return await _portainer_do(adapters, cfg, verb)
    target = "guest" if part.kind == "guest" else "service"
    ctl = build_controller(settings, adapters.proxmox, session, svc, target=target)
    if ctl is None:
        raise ControllerError("nothing to control")
    return str(await getattr(ctl, verb)())


async def _ready(
    session: Session, adapters: AdapterBundle, settings: Settings, svc: Service, part: Part
) -> tuple[bool, str]:
    """Is the thing up *and settled* (every container running, none still starting)?"""
    if part.kind in {"stack", "container"}:
        cfg = portainer_cfg(svc)
        assert cfg is not None
        portainer = adapters.portainer_for(str(cfg.get("instance") or ""))
        if portainer is None:
            return False, f"there is no Portainer connection called {cfg.get('instance')!r}"
        eid = int(cfg["endpoint"])
        try:
            if cfg.get("stack"):
                stack = await portainer.get_stack(eid, str(cfg["stack"]))
                boxes = await portainer.list_containers(eid, stack=str(cfg["stack"]))
                if stack is None or stack.status != "active":
                    return False, "stack not active yet"
            else:
                detail = await portainer.get_container(eid, str(cfg["container"]))
                boxes = [detail.container] if detail else []
        except AdapterError as exc:
            return False, str(exc)
        waiting = [
            c.name for c in boxes if c.state != "running" or c.health in {"starting", "unhealthy"}
        ]
        return (not waiting, f"waiting for {', '.join(waiting)}" if waiting else "up")
    target = "guest" if part.kind == "guest" else "service"
    ctl = build_controller(settings, adapters.proxmox, session, svc, target=target)
    if ctl is None:
        return True, ""
    try:
        st = await ctl.status()
    except ControllerError as exc:
        return False, str(exc)
    return bool(st.running), st.state


# -- the switch ---------------------------------------------------------------------------------


class _Switch:
    def __init__(
        self,
        session: Session,
        adapters: AdapterBundle,
        settings: Settings,
        actor: str,
        run: PipelineRun,
        *,
        wait_s: float,
        poll_s: float,
    ) -> None:
        self.session, self.adapters, self.settings, self.actor = session, adapters, settings, actor
        self.run = run
        self.rec = StepRecorder(session, run)
        self.wait_s, self.poll_s = wait_s, poll_s
        self.failed = False

    def step(self, name: str, status: str, **detail: Any) -> None:
        if status == "failed":
            self.failed = True
        self.rec.record(name, status, detail=detail)

    async def push_proxy(self, services: list[Service], why: str) -> None:
        hostnames = [ph.hostname for s in services for ph in proxy_hosts_of(self.session, s)]
        if not hostnames:
            return
        results = await push_registry.apply_all(
            self.session,
            self.adapters,
            names=["proxy"],
            only=hostnames,
            actor=self.actor,
        )
        bad = [r for r in results if not r.ok]
        self.step(
            f"proxy {why}",
            "failed" if bad else "ok",
            hosts=hostnames,
            results=[r.as_dict() for r in results],
        )

    async def act(self, svc: Service, part: Part, verb: str, *, wait: bool) -> bool:
        name = f"{verb} {part.label}"
        want_on = verb == "start"
        if part.on is want_on:
            self.step(name, "skipped", reason="already " + ("on" if want_on else "off"))
            return True
        started = time.monotonic()
        try:
            message = await _do(self.session, self.adapters, self.settings, svc, part, verb)
        except (ControllerError, ValueError) as exc:
            self.step(name, "failed", error=str(exc))
            return False
        if wait and want_on:
            deadline = started + self.wait_s
            while True:
                ok, why = await _ready(self.session, self.adapters, self.settings, svc, part)
                if ok:
                    break
                if time.monotonic() >= deadline:
                    self.step(name, "failed", error=f"not up in time: {why}", message=message)
                    return False
                await asyncio.sleep(self.poll_s)
        self.step(name, "ok", message=message)
        return True


def _controller_parts(state: LinkedState) -> list[Part]:
    return [p for p in state.parts if p.kind in {"stack", "container", "controller"}]


async def switch_service(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    slug: str,
    want: bool,
    *,
    actor: str,
    include_guest: bool = False,
    restore_children: bool = False,
    wait_s: float = 120.0,
    poll_s: float = 2.0,
    ignore_run: int | None = None,
) -> SwitchResult:
    """Switch ``slug`` on or off with everything linked to it. Raises
    :class:`SwitchRefused` (nothing done) or ``LookupError`` (no such service)."""
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        raise LookupError(f"no service {slug!r}")
    tree = TreeIndex.build(session)
    _check_allowed(session, svc, tree, want, ignore_run)

    verb = "on" if want else "off"
    run = PipelineRun(
        kind=PipelineKind.SWITCH,
        correlation_id=new_correlation_id(),
        service_id=svc.id,
        status=PipelineStatus.RUNNING,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    audit.record(
        session,
        actor=actor,
        action=f"service.switch_{verb}",
        target=f"service:{slug}",
        correlation_id=run.correlation_id,
        details={
            "run_id": run.id,
            "include_guest": include_guest,
            "restore_children": restore_children,
        },
    )
    session.commit()

    sw = _Switch(session, adapters, settings, actor, run, wait_s=wait_s, poll_s=poll_s)
    ctx = await gather_context(session, adapters)
    try:
        if want:
            await _switch_on(sw, svc, tree, ctx, restore_children)
        else:
            await _switch_off(sw, svc, tree, ctx, include_guest)
    except Exception as exc:  # the run must end up recorded, whatever broke
        log.warning("switch.crashed", slug=slug, error=str(exc))
        session.rollback()
        sw.step("crashed", "failed", error=f"{type(exc).__name__}: {exc}")

    session.refresh(svc)
    final = await read_linked(session, adapters, settings, svc)
    run = session.get(PipelineRun, run.id) or run
    run.finished_at = datetime.now(UTC)
    run.status = PipelineStatus.FAILED if sw.failed else PipelineStatus.SUCCEEDED
    failures = [s for s in run.steps if s["status"] == "failed"]
    if failures:
        first = failures[0]
        run.error = f"{first['name']}: {first['detail'].get('error') or 'failed'}"
    session.add(run)
    audit.record(
        session,
        actor=actor,
        action=f"service.switch_{verb}.done",
        target=f"service:{slug}",
        result=AuditResult.ERROR if sw.failed else AuditResult.OK,
        correlation_id=run.correlation_id,
        details={"run_id": run.id, "state": final.state, "error": run.error},
    )
    session.commit()
    if sw.failed:
        await announce(
            session,
            level="sub" if svc.parent_id else "service",
            key=slug,
            name=svc.name or slug,
            event="switch_failed",
            severity="error",
            title=f"Switching {slug} {verb} failed",
            body=run.error or "a step failed",
            scope={"slug": slug},
        )
    return SwitchResult(
        slug=slug,
        want=want,
        ok=not sw.failed,
        run_id=run.id,
        steps=list(run.steps),
        state=final,
        message=(
            run.error
            if sw.failed
            else (
                f"{slug} is {final.state}"
                if final.state != "none"
                else f"{slug} switched {verb} (nothing linked to read back)"
            )
        )
        or "",
    )


async def _switch_off(
    sw: _Switch, svc: Service, tree: TreeIndex, ctx: LinkedContext, include_guest: bool
) -> None:
    session = sw.session
    # children first (deepest last in `descendants`, so reversed), then the service itself
    order = [*reversed(tree.descendants(svc)), svc]
    svc.enabled = False
    svc.updated_at = datetime.now(UTC)
    session.add(svc)
    session.commit()
    resync_subtree_probes(session, sw.settings, svc)
    session.commit()
    sw.step("intent", "ok", enabled=False)

    # 1. unserve every proxy host in the subtree (the tree overlay parks them)
    await sw.push_proxy(order, "unserve")

    # 2. stack / container / controller of each, outermost first
    for s in order:
        state = await read_linked(session, sw.adapters, sw.settings, s, ctx)
        for part in _controller_parts(state):
            await sw.act(s, part, "stop", wait=False)

    # 3. the guest, only when asked and only for a root
    if svc.parent_id is None:
        state = await read_linked(session, sw.adapters, sw.settings, svc, ctx)
        for part in state.parts:
            if part.kind != "guest":
                continue
            if include_guest:
                await sw.act(svc, part, "stop", wait=False)
            else:
                sw.step(f"stop {part.label}", "skipped", reason="not confirmed (include the guest)")


async def _switch_on(
    sw: _Switch, svc: Service, tree: TreeIndex, ctx: LinkedContext, restore_children: bool
) -> None:
    session = sw.session
    state = await read_linked(session, sw.adapters, sw.settings, svc, ctx)
    if state.node_off:
        # Nothing can be started on a node that is off; waking it is a separate, deliberate step.
        sw.step(
            "host",
            "failed",
            error=f"{state.host_down} - wake it first (wake the service, or the node on Connectivity)",
        )
        return

    # 1. innermost first: the guest, then the stack / container
    for part in state.parts:
        if part.kind == "guest" and not await sw.act(svc, part, "start", wait=True):
            return
    for part in _controller_parts(state):
        if not await sw.act(svc, part, "start", wait=True):
            return

    # 2. what was asked for is now true
    svc.enabled = True
    svc.updated_at = datetime.now(UTC)
    session.add(svc)
    session.commit()
    resync_subtree_probes(session, sw.settings, svc)
    session.commit()
    sw.step("intent", "ok", enabled=True)

    # 3. children come back only if asked; otherwise they are watched, not started
    revived = [svc]
    if restore_children:
        # The guest may just have been started: read the children against what is true now.
        ctx = await gather_context(session, sw.adapters)
        for child in tree.descendants(svc):
            if not child.enabled:
                continue
            cstate = await read_linked(session, sw.adapters, sw.settings, child, ctx)
            for part in _controller_parts(cstate):
                await sw.act(child, part, "start", wait=True)
            revived.append(child)
    else:
        revived += tree.descendants(svc)

    # 4. last, serve the proxy hosts
    await sw.push_proxy(revived, "serve")
