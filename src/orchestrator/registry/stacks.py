"""Creating, deleting and controlling Portainer stacks - through Portainer's API.

The orchestrator never keeps a stack: it asks Portainer to create or delete one and keeps only
the link and policies it adds. Creating is a two-step action (a preview of the exact request with
the secrets masked, then a confirmed create); deleting asks for the stack's name and refuses while
something linked to it is still switched on. Environment values and Git passwords go straight to
Portainer and are never stored, logged or audited here.
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


def _adapter(adapters: AdapterBundle) -> PortainerAdapter:
    adapter = adapters.portainer
    if adapter is None or not adapter.configured:
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
) -> str:
    """start | stop | restart one stack or one container, audited."""
    if action not in {"start", "stop", "restart"}:
        raise StackError(f"unknown action {action!r}", 400)
    adapter = _adapter(adapters)
    try:
        if kind == "stack":
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


def preview(adapters: AdapterBundle, spec: StackCreate) -> dict[str, Any]:
    """The request that would be sent to Portainer, secrets masked."""
    adapter = _adapter(adapters)
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
) -> CreateResult:
    adapter = _adapter(adapters)
    shown = preview(adapters, spec)
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
) -> DeleteResult:
    """Delete the stack in Portainer (volumes are kept), then remove the link."""
    from orchestrator.db.models import ProxyHost
    from orchestrator.web.routers import dashboard as api

    adapter = _adapter(adapters)
    if confirm_name != name:
        raise StackError(f"type the stack's name ({name!r}) to confirm deleting it", 400)
    stack = await _stack(adapter, endpoint_id, name)
    result = DeleteResult(stack=stack.name)

    roots = linked_services(session, endpoint_id, stack.name)
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
