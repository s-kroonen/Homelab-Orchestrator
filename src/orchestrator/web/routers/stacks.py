"""Adopt, create, delete and control Portainer stacks; link proxy hosts to a service.

Portainer owns stacks. These endpoints ask Portainer (through its API) to create or delete one
and keep only the link and policies on this side. See docs/portainer_deploys.md.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from orchestrator.adapters.portainer.base import StackCreate
from orchestrator.db.models import ProxyHost, Service
from orchestrator.registry import adopt as adoption
from orchestrator.registry import stacks as stack_ops
from orchestrator.registry.adopt import AdoptPlan, ChildPlan, StackError
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["stacks"], dependencies=[Depends(require_admin)])


def fail(exc: StackError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


class ChildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container: str
    slug: str
    name: str = ""
    backup_gate: Literal["ignore", "required"] = "ignore"


class AdoptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: int
    stack: str
    slug: str
    parent: str
    name: str = ""
    group: str = ""
    children: list[ChildIn] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list)
    probes: bool = True
    deploy: Literal["none", "probes", "probes+backup"] | None = None
    webhook: Literal["generic", "gitlab", "github"] | None = None

    def plan(self) -> AdoptPlan:
        return AdoptPlan(
            endpoint_id=self.endpoint_id,
            stack=self.stack,
            slug=self.slug,
            parent=self.parent,
            name=self.name,
            group=self.group,
            children=[ChildPlan(**c.model_dump()) for c in self.children],
            hosts=self.hosts,
            probes=self.probes,
            deploy=self.deploy,
            webhook=self.webhook,
        )


class StackCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: int
    name: str
    source: Literal["git", "string"] = "git"
    repository_url: str = ""
    reference: str = "refs/heads/main"
    compose_path: str = "docker-compose.yml"
    git_credential_id: int | None = None
    git_username: str = ""
    git_password: str = ""
    auto_update: bool = False
    auto_update_interval: str = ""
    compose_text: str = ""
    #: environment variables, sent to Portainer and never kept here
    env: dict[str, str] = Field(default_factory=dict)

    def spec(self) -> StackCreate:
        data = self.model_dump()
        data["env"] = tuple(self.env.items())
        return StackCreate(**data)


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: StackCreateIn
    #: must be true: the preview has been looked at
    confirm: bool = False
    #: also link the new stack as a sub-service (``stack`` and ``endpoint_id`` are filled in)
    adopt: AdoptIn | None = None


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_name: str
    take_down: bool = False
    remove_services: bool = True


# -- adopt ---------------------------------------------------------------------------------------


@router.get("/stacks/suggest")
async def suggest(
    endpoint: int, name: str, session: SessionDep, adapters: AdaptersDep, parent: str | None = None
) -> dict[str, Any]:
    try:
        return (await adoption.suggest(session, adapters, endpoint, name, parent=parent)).as_dict()
    except StackError as exc:
        raise fail(exc) from exc


@router.get("/stacks/parents")
async def parents(session: SessionDep) -> dict[str, Any]:
    """The services a new stack can be linked under (roots that own a guest)."""
    return {"parents": adoption.parent_candidates(session)}


@router.post("/stacks/adopt", status_code=status.HTTP_201_CREATED)
async def adopt(
    body: AdoptIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    try:
        result = await adoption.adopt(session, adapters, settings, admin.username, body.plan())
    except StackError as exc:
        raise fail(exc) from exc
    return result.as_dict()


# -- create ----------------------------------------------------------------------------------------


@router.post("/stacks/preview")
async def preview(body: StackCreateIn, adapters: AdaptersDep) -> dict[str, Any]:
    """The exact request that would go to Portainer, with passwords and values masked."""
    try:
        return stack_ops.preview(adapters, body.spec())
    except StackError as exc:
        raise fail(exc) from exc


@router.post("/stacks", status_code=status.HTTP_201_CREATED)
async def create(
    body: CreateIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "confirm: true is required - look at /api/stacks/preview first",
        )
    try:
        result = await stack_ops.create(
            session,
            adapters,
            settings,
            admin.username,
            body.spec.spec(),
            adopt_plan=body.adopt.plan() if body.adopt else None,
        )
    except StackError as exc:
        raise fail(exc) from exc
    return {
        "stack": {
            "name": result.stack.name,
            "id": result.stack.id,
            "endpoint_id": result.stack.endpoint_id,
        },
        "request": result.request,
        "adopted": result.adopted.as_dict() if result.adopted else None,
        "warnings": result.warnings,
    }


# -- delete / control ----------------------------------------------------------------------------


@router.delete("/stacks/{endpoint_id}/{name}")
async def delete(
    endpoint_id: int,
    name: str,
    body: DeleteIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    try:
        result = await stack_ops.delete(
            session,
            adapters,
            settings,
            admin.username,
            endpoint_id,
            name,
            confirm_name=body.confirm_name,
            take_down=body.take_down,
            remove_services=body.remove_services,
        )
    except StackError as exc:
        raise fail(exc) from exc
    return result.__dict__


@router.post("/stacks/{endpoint_id}/{name}/{action}")
async def stack_action(
    endpoint_id: int,
    name: str,
    action: Literal["start", "stop", "restart"],
    session: SessionDep,
    adapters: AdaptersDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    try:
        message = await stack_ops.control(
            session,
            adapters,
            admin.username,
            kind="stack",
            endpoint_id=endpoint_id,
            ref=name,
            action=action,
        )
    except StackError as exc:
        raise fail(exc) from exc
    return {"message": message}


@router.post("/containers/{endpoint_id}/{ref}/{action}")
async def container_action(
    endpoint_id: int,
    ref: str,
    action: Literal["start", "stop", "restart"],
    session: SessionDep,
    adapters: AdaptersDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    try:
        message = await stack_ops.control(
            session,
            adapters,
            admin.username,
            kind="container",
            endpoint_id=endpoint_id,
            ref=ref,
            action=action,
        )
    except StackError as exc:
        raise fail(exc) from exc
    return {"message": message}


# -- proxy hosts, from the service side ---------------------------------------------------------------


def _host(session: SessionDep, hostname: str) -> ProxyHost:
    row = session.exec(select(ProxyHost).where(ProxyHost.hostname == hostname)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no proxy host {hostname!r}")
    return row


def _service(session: SessionDep, slug: str) -> Service:
    row = session.exec(select(Service).where(Service.slug == slug)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    return row


class LinkHostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    #: move it from the service it is on now
    force: bool = False


@router.get("/services/{slug}/proxy-host-candidates")
async def host_candidates(slug: str, session: SessionDep, adapters: AdaptersDep) -> dict[str, Any]:
    """Proxy hosts whose upstream points at a port this service's stack publishes."""
    svc = _service(session, slug)
    cfg = svc.controller or {}
    if cfg.get("type") != "portainer" or not cfg.get("stack"):
        return {"slug": slug, "candidates": [], "linked": _linked(session, svc)}
    try:
        found = await adoption.suggest(session, adapters, int(cfg["endpoint"]), str(cfg["stack"]))
    except StackError as exc:
        raise fail(exc) from exc
    return {"slug": slug, "candidates": found.hosts, "linked": _linked(session, svc)}


def _linked(session: SessionDep, svc: Service) -> list[str]:
    return [
        ph.hostname
        for ph in session.exec(select(ProxyHost).where(ProxyHost.service_id == svc.id)).all()
    ]


@router.post("/services/{slug}/proxy-hosts")
async def link_host(
    slug: str, body: LinkHostIn, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    svc, ph = _service(session, slug), _host(session, body.hostname)
    if ph.service_id not in (None, svc.id) and not body.force:
        other = session.get(Service, ph.service_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{ph.hostname} is linked to {other.slug if other else ph.service_id} - "
            "say to move it (force) or unlink it there first",
        )
    adoption.link_host(session, settings, admin.username, ph, svc)
    session.commit()
    return {"slug": slug, "hostnames": _linked(session, svc)}


@router.delete("/services/{slug}/proxy-hosts/{hostname}")
async def unlink_host(
    slug: str, hostname: str, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    svc, ph = _service(session, slug), _host(session, hostname)
    if ph.service_id != svc.id:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{hostname} is not linked to {slug}")
    adoption.link_host(session, settings, admin.username, ph, None)
    session.commit()
    return {"slug": slug, "hostnames": _linked(session, svc)}
