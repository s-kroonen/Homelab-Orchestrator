"""Creating, deleting and controlling Portainer stacks - through Portainer's API.

The orchestrator never keeps a stack: it asks Portainer to create or delete one and keeps only
the link and policies it adds. Creating is a two-step action (a preview of the exact request with
the secrets masked, then a confirmed create); deleting asks for the stack's name and refuses while
something linked to it is still switched on. Environment values and Git passwords go straight to
Portainer and are never stored, logged or audited here.

The stack page (``/portainer/<environment>/<stack>``) also reads and changes a stack's
environment: values whose name looks secret are only sent when someone reveals one (audited by
name), and a change redeploys the stack (audited by names: added, changed, removed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.portainer.base import PortainerAdapter, Stack, StackCreate
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import Service
from orchestrator.domain.enums import AuditResult
from orchestrator.registry import adopt as adoption
from orchestrator.registry.adopt import AdoptPlan, StackError, linked_services
from orchestrator.registry.switch import SwitchRefused, switch_service
from orchestrator.registry.tree import TreeIndex


def _adapter(adapters: AdapterBundle, instance: str | None = None) -> PortainerAdapter:
    adapter = adapters.portainer_for(instance)
    if adapter is None:
        raise StackError(f"there is no Portainer connection called {instance!r}", 404)
    if not adapter.configured:
        raise StackError("Portainer is not set up", 503)
    return adapter


async def _stack(adapter: PortainerAdapter, endpoint_id: int, name: str) -> Stack:
    try:
        stack = await adapter.get_stack(endpoint_id, name)
    except AdapterError as exc:
        raise StackError(str(exc), 502) from exc
    if stack is None:
        raise StackError(f"no stack {name!r} on Portainer environment {endpoint_id}", 404)
    return stack


# -- control -----------------------------------------------------------------------------------


async def control(
    session: Session,
    adapters: AdapterBundle,
    actor: str,
    *,
    kind: str,
    endpoint_id: int,
    ref: str,
    action: str,
    instance: str | None = None,
) -> str:
    """start | stop | restart | redeploy one stack, or start | stop | restart | kill | recreate
    one container, audited."""
    allowed = {"start", "stop", "restart"} | (
        {"redeploy"} if kind == "stack" else {"kill", "recreate"}
    )
    if action not in allowed:
        raise StackError(f"unknown action {action!r}", 400)
    adapter = _adapter(adapters, instance)
    try:
        if kind == "stack" and action == "redeploy":
            stack = await _stack(adapter, endpoint_id, ref)
            await adapter.redeploy_stack(stack, pull_image=True)
            message = (
                f"stack {stack.name} redeployed from {stack.git.reference if stack.git else 'Git'}"
            )
        elif kind == "stack":
            stack = await _stack(adapter, endpoint_id, ref)
            if action in {"stop", "restart"}:
                await adapter.stop_stack(stack)
            if action in {"start", "restart"}:
                await adapter.start_stack(stack)
            message = f"{action} done for stack {stack.name}"
        else:
            message = await adapter.container_action(endpoint_id, ref, action)
    except AdapterError as exc:
        audit.record(
            session,
            actor=actor,
            action=f"{kind}.{action}",
            target=f"portainer:{endpoint_id}/{ref}",
            result=AuditResult.ERROR,
            details={"error": str(exc)},
        )
        session.commit()
        raise StackError(str(exc), 502) from exc
    audit.record(
        session,
        actor=actor,
        action=f"{kind}.{action}",
        target=f"portainer:{endpoint_id}/{ref}",
        details={"message": message},
    )
    session.commit()
    return message


# -- create ----------------------------------------------------------------------------------------


def preview(
    adapters: AdapterBundle, spec: StackCreate, instance: str | None = None
) -> dict[str, Any]:
    """The request that would be sent to Portainer, secrets masked."""
    adapter = _adapter(adapters, instance)
    try:
        return adapter.preview_create(spec)
    except AdapterError as exc:
        raise StackError(str(exc), 400) from exc


@dataclass
class CreateResult:
    stack: Stack
    request: dict[str, Any]
    adopted: adoption.AdoptResult | None = None
    warnings: list[str] = field(default_factory=list)


async def create(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    actor: str,
    spec: StackCreate,
    *,
    adopt_plan: AdoptPlan | None = None,
    instance: str | None = None,
) -> CreateResult:
    adapter = _adapter(adapters, instance)
    shown = preview(adapters, spec, instance)
    try:
        if await adapter.get_stack(spec.endpoint_id, spec.name) is not None:
            raise StackError(f"a stack named {spec.name!r} already exists there", 409)
        stack = await adapter.create_stack(spec)
    except AdapterError as exc:
        audit.record(
            session,
            actor=actor,
            action="stack.create",
            target=f"portainer:{spec.endpoint_id}/{spec.name}",
            result=AuditResult.ERROR,
            details={"request": shown, "error": str(exc)},
        )
        session.commit()
        raise StackError(str(exc), 502) from exc
    audit.record(
        session,
        actor=actor,
        action="stack.create",
        target=f"portainer:{spec.endpoint_id}/{spec.name}",
        details={"request": shown},
    )
    session.commit()
    result = CreateResult(stack=stack, request=shown)
    if adopt_plan is not None:
        adopt_plan.stack = stack.name
        adopt_plan.endpoint_id = spec.endpoint_id
        adopt_plan.instance = adapter.instance_id
        try:
            result.adopted = await adoption.adopt(session, adapters, settings, actor, adopt_plan)
        except StackError as exc:
            result.warnings.append(
                f"the stack was created in Portainer but could not be linked here: {exc}"
            )
    return result


# -- delete ----------------------------------------------------------------------------------------


@dataclass
class DeleteResult:
    stack: str
    taken_down: list[str] = field(default_factory=list)
    removed_services: list[str] = field(default_factory=list)
    unlinked_hosts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def delete(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    actor: str,
    endpoint_id: int,
    name: str,
    *,
    confirm_name: str,
    take_down: bool = False,
    remove_services: bool = True,
    instance: str | None = None,
) -> DeleteResult:
    """Delete the stack in Portainer (volumes are kept), then remove the link."""
    from orchestrator.db.models import ProxyHost
    from orchestrator.web.routers import dashboard as api

    adapter = _adapter(adapters, instance)
    if confirm_name != name:
        raise StackError(f"type the stack's name ({name!r}) to confirm deleting it", 400)
    stack = await _stack(adapter, endpoint_id, name)
    result = DeleteResult(stack=stack.name)

    roots = linked_services(
        session,
        endpoint_id,
        stack.name,
        instance=adapter.instance_id,
        default=adapters.default_portainer,
    )
    tree = TreeIndex.build(session)
    below = {d.id: d for r in roots for d in tree.descendants(r)}
    linked = [*roots, *below.values()]
    in_use = [s for s in roots if tree.is_served(s)]
    if in_use and not take_down:
        raise StackError(
            f"{', '.join(s.slug for s in in_use)} is switched on - switch it off first "
            "(or say to take it down as part of the delete)",
            409,
        )
    for svc in in_use:
        try:
            switched = await switch_service(
                session, adapters, settings, svc.slug, False, actor=actor
            )
        except (SwitchRefused, LookupError) as exc:
            raise StackError(f"could not take {svc.slug} down first: {exc}", 409) from exc
        if not switched.ok:
            raise StackError(
                f"could not take {svc.slug} down first: {switched.message} - nothing was deleted",
                409,
            )
        result.taken_down.append(svc.slug)

    try:
        await adapter.delete_stack(stack)
    except AdapterError as exc:
        audit.record(
            session,
            actor=actor,
            action="stack.delete",
            target=f"portainer:{endpoint_id}/{name}",
            result=AuditResult.ERROR,
            details={"error": str(exc)},
        )
        session.commit()
        raise StackError(str(exc), 502) from exc

    hosts_before = {
        ph.hostname
        for ph in session.exec(select(ProxyHost)).all()
        if ph.service_id in {s.id for s in linked}
    }
    for svc in sorted(linked, key=lambda s: -tree.depth(s)):
        try:
            if remove_services:
                await _remove(api, session, svc.slug, actor)
                result.removed_services.append(svc.slug)
            else:
                svc = session.exec(select(Service).where(Service.slug == svc.slug)).one()
                svc.controller, svc.deploy = {}, {}
                session.add(svc)
                session.commit()
        except StackError as exc:
            result.warnings.append(f"{svc.slug} was kept: {exc}")
            svc = session.exec(select(Service).where(Service.slug == svc.slug)).one()
            svc.controller, svc.deploy = {}, {}
            session.add(svc)
            session.commit()
    result.unlinked_hosts = sorted(hosts_before)
    audit.record(
        session,
        actor=actor,
        action="stack.delete",
        target=f"portainer:{endpoint_id}/{name}",
        details={
            "taken_down": result.taken_down,
            "removed_services": result.removed_services,
            "unlinked_hosts": result.unlinked_hosts,
        },
    )
    session.commit()
    return result


async def _remove(api: Any, session: Session, slug: str, actor: str) -> None:
    try:
        await api.delete_service(slug, session, SimpleNamespace(username=actor), cascade=True)
    except HTTPException as exc:
        raise StackError(str(exc.detail), exc.status_code) from exc


# -- the stack page: environment and Git settings ---------------------------------------------------

_SECRET = (
    "PASS",
    "SECRET",
    "TOKEN",
    "KEY",
    "PRIVATE",
    "CREDENTIAL",
    "DSN",
    "AUTH",
    "COOKIE",
    "SALT",
)


def looks_secret(name: str) -> bool:
    """A variable whose value is hidden until someone reveals it."""
    upper = name.upper()
    return any(word in upper for word in _SECRET)


async def env_view(
    adapters: AdapterBundle, endpoint_id: int, name: str, instance: str | None = None
) -> dict[str, Any]:
    """The stack's environment for the page: each variable's name, its value unless it looks
    secret, and which containers have it."""
    adapter = _adapter(adapters, instance)
    stack = await _stack(adapter, endpoint_id, name)
    if stack.id is None:
        return {
            "editable": False,
            "vars": [],
            "why": "Portainer did not create this stack, so it has no environment of its own.",
        }
    try:
        env = await adapter.stack_env(stack)
        boxes = await adapter.list_containers(endpoint_id, stack=stack.name)
        details = [await adapter.get_container(endpoint_id, c.name) for c in boxes]
    except AdapterError as exc:
        raise StackError(str(exc), 502) from exc
    used: dict[str, list[str]] = {}
    for d in details:
        if d is None:
            continue
        for n in d.env_names:
            used.setdefault(n, []).append(d.container.name)
    return {
        "editable": True,
        "vars": [
            {
                "name": n,
                "secret": looks_secret(n),
                "value": None if looks_secret(n) else v,
                "used_by": used.get(n, []),
            }
            for n, v in env
        ],
        "why": "",
    }


async def reveal_env(
    session: Session,
    adapters: AdapterBundle,
    actor: str,
    endpoint_id: int,
    name: str,
    var: str,
    instance: str | None = None,
) -> str:
    """One variable's value, on purpose. Audited (the name, never the value)."""
    adapter = _adapter(adapters, instance)
    stack = await _stack(adapter, endpoint_id, name)
    try:
        env = dict(await adapter.stack_env(stack))
    except AdapterError as exc:
        raise StackError(str(exc), 502) from exc
    if var not in env:
        raise StackError(f"stack {name!r} has no variable {var!r}", 404)
    audit.record(
        session,
        actor=actor,
        action="stack.env_revealed",
        target=f"portainer:{endpoint_id}/{stack.name}",
        details={"variable": var},
    )
    session.commit()
    return env[var]


async def set_env(
    session: Session,
    adapters: AdapterBundle,
    actor: str,
    endpoint_id: int,
    name: str,
    wanted: list[tuple[str, str | None]],
    instance: str | None = None,
) -> dict[str, list[str]]:
    """Replace the stack's environment with ``wanted`` (a value None keeps the current one, so a
    hidden secret need not be revealed to keep it) and redeploy. Audited by names."""
    adapter = _adapter(adapters, instance)
    stack = await _stack(adapter, endpoint_id, name)
    if stack.id is None:
        raise StackError(
            "Portainer did not create this stack, so its environment is not Portainer's to change",
            409,
        )
    seen: set[str] = set()
    for var, _ in wanted:
        if not var or not var.replace("_", "").isalnum() or var[0].isdigit():
            raise StackError(f"{var!r} is not a variable name (letters, digits and _)", 400)
        if var in seen:
            raise StackError(f"{var} is listed twice", 400)
        seen.add(var)
    try:
        current = dict(await adapter.stack_env(stack))
    except AdapterError as exc:
        raise StackError(str(exc), 502) from exc
    new: list[tuple[str, str]] = []
    for var, value in wanted:
        if value is None:
            if var not in current:
                raise StackError(f"{var} has no value to keep", 400)
            value = current[var]
        new.append((var, value))
    after = dict(new)
    change = {
        "added": sorted(set(after) - set(current)),
        "changed": sorted(k for k in after if k in current and after[k] != current[k]),
        "removed": sorted(set(current) - set(after)),
    }
    if not any(change.values()):
        return change
    try:
        await adapter.set_stack_env(stack, new, pull_image=False)
    except AdapterError as exc:
        audit.record(
            session,
            actor=actor,
            action="stack.env_changed",
            target=f"portainer:{endpoint_id}/{stack.name}",
            result=AuditResult.ERROR,
            details={**change, "error": str(exc)},
        )
        session.commit()
        raise StackError(str(exc), 502) from exc
    audit.record(
        session,
        actor=actor,
        action="stack.env_changed",
        target=f"portainer:{endpoint_id}/{stack.name}",
        details=change,
    )
    session.commit()
    return change


async def set_git(
    session: Session,
    adapters: AdapterBundle,
    actor: str,
    endpoint_id: int,
    name: str,
    *,
    reference: str,
    auto_update_interval: str | None,
    instance: str | None = None,
) -> str:
    """A Git stack's branch / tag and Portainer's own polling. Audited."""
    import re

    if auto_update_interval and not re.fullmatch(r"\d+[smh]", auto_update_interval):
        raise StackError("the interval is a number and s, m or h, like 5m", 400)
    if not reference.strip():
        raise StackError("the branch or tag is needed (refs/heads/main)", 400)
    adapter = _adapter(adapters, instance)
    stack = await _stack(adapter, endpoint_id, name)
    try:
        await adapter.set_git_settings(
            stack, reference=reference.strip(), auto_update_interval=auto_update_interval or None
        )
    except AdapterError as exc:
        raise StackError(str(exc), 502) from exc
    audit.record(
        session,
        actor=actor,
        action="stack.git_settings",
        target=f"portainer:{endpoint_id}/{stack.name}",
        details={"reference": reference.strip(), "auto_update": auto_update_interval or "off"},
    )
    session.commit()
    return f"{stack.name}: {reference.strip()}, " + (
        f"Portainer checks every {auto_update_interval}" if auto_update_interval else "no polling"
    )


def link_view(
    session: Session,
    adapters: AdapterBundle,
    endpoint_id: int,
    name: str,
    instance: str | None = None,
) -> dict[str, Any] | None:
    """What the orchestrator keeps about a stack (the sub-service linked to it, its container
    checks and their last results), from the database only. None when nothing links to it."""
    from orchestrator.db.models import Probe, ServiceState
    from orchestrator.registry.stack_fold import container_of

    adapter = _adapter(adapters, instance)
    found = linked_services(
        session, endpoint_id, name, instance=adapter.instance_id, default=adapters.default_portainer
    )
    if not found:
        return None
    svc = found[0]
    tree = TreeIndex.build(session)
    parent = tree.parent(svc)
    state = session.get(ServiceState, svc.id)
    results = {str(r.get("probe_name")): r for r in (state.last_probe_results if state else [])}
    checks = []
    for p in session.exec(select(Probe).where(Probe.service_id == svc.id).order_by(Probe.order)):
        r = results.get(p.name) or {}
        checks.append(
            {
                "id": p.id,
                "name": p.name,
                "container": container_of(p),
                "required": p.required,
                "enabled": p.enabled,
                "backup_waits": p.backup_waits,
                "state": "skipped" if not p.enabled else r.get("state") or "not run",
                "message": r.get("message") or "",
            }
        )
    deploy = svc.deploy or {}
    return {
        "slug": svc.slug,
        "name": svc.name or svc.slug,
        "parent": {"slug": parent.slug, "name": parent.name or parent.slug} if parent else None,
        "verdict": state.last_verdict.value if state else "unknown",
        "enabled": svc.enabled,
        "backup_gate": svc.backup_gate.value,
        "deploy": {
            "enabled": bool(deploy) and bool(deploy.get("enabled", True)),
            "pull_image": bool(deploy.get("pull_image", True)),
            "prune": bool(deploy.get("prune", False)),
            "webhook": bool((deploy.get("trigger") or {}).get("webhook")),
        },
        "checks": checks,
        "others": [s.slug for s in found[1:]],
    }
