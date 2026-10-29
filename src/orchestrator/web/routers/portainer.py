"""Read-only view of Portainer: environments, stacks, containers, logs, stats.

Portainer owns stacks; this only reads them (redacted: no environment values, no Git
credentials, no webhook ids) and says which sub-service, if any, is linked to each. Every
object carries an ``open_url`` into Portainer's own UI and the host commands that do the same
by hand - the orchestrator is one control point among several, not the only one.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from orchestrator.adapters.errors import (
    AdapterAuthError,
    AdapterError,
    AdapterNotConfigured,
    AdapterRequestError,
    AdapterUnreachable,
)
from orchestrator.adapters.portainer.base import Container, PortainerAdapter, Stack
from orchestrator.adapters.portainer.links import container_url, env_url, stack_url
from orchestrator.db.models import Service
from orchestrator.web.auth import get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep
from orchestrator.web.templating import templates

api = APIRouter(prefix="/api/portainer", tags=["portainer"], dependencies=[Depends(require_admin)])
pages = APIRouter(tags=["portainer"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = templates

#: How many stack compose files are read at once when labelling image/build.
_KIND_CONCURRENCY = 4


def _adapter(adapters: AdaptersDep, instance: str | None = None) -> PortainerAdapter:
    adapter = adapters.portainer_for(instance)
    if adapter is None and instance:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"there is no Portainer connection called {instance!r}"
        )
    if adapter is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Portainer is not set up")
    return adapter


def _fail(exc: AdapterError) -> HTTPException:
    if isinstance(exc, AdapterNotConfigured):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if isinstance(exc, AdapterAuthError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    if isinstance(exc, AdapterRequestError) and exc.status_code == 404:
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, AdapterUnreachable):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


_ANSWERED = re.compile(r"Portainer answered (\d{3})")


def _why(name: str | None, detail: str) -> str:
    """One line for the page: which environment, and that it was skipped. The full text (an
    error page's markup, a stack trace) stays in the logs and the CLI."""
    answered = _ANSWERED.search(detail)
    if answered:
        reason = f"Portainer answered {answered.group(1)}"
    elif "unreachable" in detail or "timed out" in detail:
        reason = "did not answer"
    else:
        reason = detail.split("\n")[0][:80]
    label = f"{name} - " if name else ""
    return f"{label}unreachable, hidden ({reason})"


# -- links the orchestrator keeps (and nothing else about a stack) ------------------------


def _links(
    session: Session, adapter: PortainerAdapter, default: str
) -> dict[tuple[str, int, str], list[str]]:
    """(kind, environment, name) -> slugs of the sub-services linked to it, on this Portainer
    (a link naming no connection is on the default one)."""
    out: dict[tuple[str, int, str], list[str]] = {}
    for svc in session.exec(select(Service)).all():
        cfg = svc.controller or {}
        if cfg.get("type") != "portainer":
            continue
        if (str(cfg.get("instance") or "") or default) != adapter.instance_id:
            continue
        try:
            eid = int(cfg["endpoint"])
        except (KeyError, TypeError, ValueError):
            continue
        for kind in ("stack", "container"):
            if cfg.get(kind):
                out.setdefault((kind, eid, str(cfg[kind]).lower()), []).append(svc.slug)
    return out


# -- "open in Portainer" and the same thing by hand ----------------------------------------


def _stack_view(
    s: Stack,
    base: str,
    links: dict[tuple[str, int, str], list[str]],
    containers: list[Container] | None = None,
    kind: str | None = None,
    instance: str = "",
) -> dict[str, Any]:
    data = asdict(s)
    data["instance"] = instance
    data["open_url"] = stack_url(base, s)
    data["linked_services"] = links.get(("stack", s.endpoint_id, s.name.lower()), [])
    data["host_commands"] = [
        f"docker compose -p {s.name} ps",
        f"docker compose -p {s.name} logs --tail 200",
    ]
    if kind is not None:
        data["kind"] = kind
    if containers is not None:
        data["containers_total"] = len(containers)
        data["containers_running"] = sum(1 for c in containers if c.state == "running")
        data["container_states"] = [
            {"name": c.name, "state": c.state, "health": c.health} for c in containers
        ]
    return data


def _container_view(
    c: Container,
    eid: int,
    base: str,
    links: dict[tuple[str, int, str], list[str]],
    instance: str = "",
) -> dict[str, Any]:
    data = asdict(c)
    data["instance"] = instance
    data["endpoint_id"] = eid
    data["open_url"] = container_url(base, eid, c.id)
    data["linked_services"] = links.get(("container", eid, c.name.lower()), [])
    return data


# -- API ----------------------------------------------------------------------------------------


@api.get("/instances")
async def instances(adapters: AdaptersDep) -> list[dict[str, Any]]:
    """The Portainer connections (Connections page): what the page's picker offers."""
    return [
        {
            "id": a.instance_id,
            "name": a.instance_name,
            "default": a.instance_id == adapters.default_portainer,
            "configured": a.configured,
            "url": a.base_url,
        }
        for a in adapters.portainers.values()
    ]


@api.get("/status")
async def portainer_status(
    adapters: AdaptersDep,
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> dict[str, Any]:
    adapter = _adapter(adapters, instance)
    check = await adapter.check()
    return {**asdict(check), "url": adapter.base_url, "instance": adapter.instance_id}


@api.get("/environments")
async def environments(
    adapters: AdaptersDep,
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> list[dict[str, Any]]:
    adapter = _adapter(adapters, instance)
    try:
        envs = await adapter.list_environments()
    except AdapterError as exc:
        raise _fail(exc) from exc
    return [{**asdict(e), "open_url": env_url(adapter.base_url, e.id)} for e in envs]


@api.get("/stacks")
async def stacks(
    adapters: AdaptersDep,
    session: SessionDep,
    endpoint: int | None = Query(None, description="Only this environment"),
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> dict[str, Any]:
    adapter = _adapter(adapters, instance)
    links = _links(session, adapter, adapters.default_portainer)
    try:
        listing = await adapter.list_stacks(endpoint)
    except AdapterError as exc:
        raise _fail(exc) from exc
    # An environment that cannot be read (an agent that is down, a 502 from it, a guest that is
    # off) must not take the others with it, and its stacks must not be shown as if they were
    # alive: they are left out, and one line says which environments were.
    env_ids = sorted({s.endpoint_id for s in listing.stacks})
    try:
        envs = await adapter.list_environments()
    except AdapterError:
        envs = []
    names = {e.id: e.name for e in envs}
    down = {e.id for e in envs if e.status == "down"}
    for eid in down & set(env_ids):  # Portainer already knows: no point asking it for containers
        listing.errors.setdefault(eid, "Portainer reports it as down")
    env_ids = [e for e in env_ids if e not in down and e not in listing.errors]
    per_env = await asyncio.gather(
        *(adapter.list_containers(e) for e in env_ids), return_exceptions=True
    )
    containers: dict[int, list[Container]] = {}
    unreadable: set[int] = set(down) | set(listing.errors)
    for eid, result in zip(env_ids, per_env, strict=True):
        if isinstance(result, AdapterError):
            unreadable.add(eid)
            listing.errors.setdefault(eid, str(result))
        elif isinstance(result, BaseException):
            raise result
        else:
            containers[eid] = result
    listing.stacks = [s for s in listing.stacks if s.endpoint_id not in unreadable]
    gate = asyncio.Semaphore(_KIND_CONCURRENCY)

    async def kind_of(s: Stack) -> str:
        async with gate:
            return await adapter.stack_kind(s)

    kinds = await asyncio.gather(*(kind_of(s) for s in listing.stacks))
    base = adapter.base_url
    rows = []
    for s, kind in zip(listing.stacks, kinds, strict=True):
        mine = [
            c
            for c in containers.get(s.endpoint_id, [])
            if (c.stack or "").lower() == s.name.lower()
        ]
        rows.append(_stack_view(s, base, links, mine, kind, adapter.instance_id))
    # Containers outside any compose project: one "(no stack)" row per environment.
    loose = []
    for eid, boxes in sorted(containers.items()):
        mine = [c for c in boxes if not c.stack]
        if mine:
            loose.append(
                {
                    "endpoint_id": eid,
                    "instance": adapter.instance_id,
                    "containers_total": len(mine),
                    "containers_running": sum(1 for c in mine if c.state == "running"),
                    "container_states": [
                        {"name": c.name, "state": c.state, "health": c.health} for c in mine
                    ],
                    "linked_services": sorted(
                        {x for c in mine for x in links.get(("container", eid, c.name.lower()), [])}
                    ),
                }
            )
    return {
        "instance": adapter.instance_id,
        "stacks": rows,
        "loose": loose,
        "errors": {str(k): _why(names.get(k), v) for k, v in listing.errors.items()},
        "skipped": sorted(listing.errors),
    }


@api.get("/endpoints/{endpoint_id}/stacks/{name}")
async def stack_detail(
    endpoint_id: int,
    name: str,
    adapters: AdaptersDep,
    session: SessionDep,
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> dict[str, Any]:
    adapter = _adapter(adapters, instance)
    links = _links(session, adapter, adapters.default_portainer)
    try:
        found = await adapter.get_stack(endpoint_id, name)
        if found is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no stack {name!r} on environment {endpoint_id}"
            )
        containers = await adapter.list_containers(endpoint_id, stack=found.name)
        kind = await adapter.stack_kind(found)
    except AdapterError as exc:
        raise _fail(exc) from exc
    base = adapter.base_url
    view = _stack_view(found, base, links, containers, kind, adapter.instance_id)
    view["containers"] = [
        _container_view(c, endpoint_id, base, links, adapter.instance_id) for c in containers
    ]
    return view


@api.get("/endpoints/{endpoint_id}/containers")
async def containers(
    endpoint_id: int,
    adapters: AdaptersDep,
    session: SessionDep,
    stack: str | None = Query(None),
    all: bool = Query(True, description="Include stopped containers"),
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> list[dict[str, Any]]:
    adapter = _adapter(adapters, instance)
    links = _links(session, adapter, adapters.default_portainer)
    try:
        rows = await adapter.list_containers(endpoint_id, stack=stack, include_stopped=all)
    except AdapterError as exc:
        raise _fail(exc) from exc
    return [
        _container_view(c, endpoint_id, adapter.base_url, links, adapter.instance_id) for c in rows
    ]


@api.get("/endpoints/{endpoint_id}/containers/{ref}")
async def container_detail(
    endpoint_id: int,
    ref: str,
    adapters: AdaptersDep,
    session: SessionDep,
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> dict[str, Any]:
    adapter = _adapter(adapters, instance)
    try:
        detail = await adapter.get_container(endpoint_id, ref)
    except AdapterError as exc:
        raise _fail(exc) from exc
    if detail is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no container {ref!r} on environment {endpoint_id}"
        )
    view = asdict(detail)
    view["container"] = _container_view(
        detail.container,
        endpoint_id,
        adapter.base_url,
        _links(session, adapter, adapters.default_portainer),
        adapter.instance_id,
    )
    name = detail.container.name
    view["host_commands"] = [
        f"docker logs --tail 200 -f {name}",
        f"docker inspect {name}",
        f"docker restart {name}",
    ]
    return view


@api.get("/endpoints/{endpoint_id}/containers/{ref}/inspect")
async def container_inspect(
    endpoint_id: int,
    ref: str,
    adapters: AdaptersDep,
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> dict[str, Any]:
    """Docker's own description of the container, environment values masked."""
    adapter = _adapter(adapters, instance)
    try:
        raw = await adapter.inspect(endpoint_id, ref)
    except AdapterError as exc:
        raise _fail(exc) from exc
    if raw is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no container {ref!r} on environment {endpoint_id}"
        )
    return raw


@api.get("/endpoints/{endpoint_id}/containers/{ref}/logs")
async def container_logs(
    endpoint_id: int,
    ref: str,
    adapters: AdaptersDep,
    tail: int = Query(200, ge=1, le=2000),
    since: int | None = Query(None, ge=0, description="Unix seconds"),
    timestamps: bool = Query(True),
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> dict[str, Any]:
    adapter = _adapter(adapters, instance)
    try:
        lines = await adapter.logs(endpoint_id, ref, tail=tail, since=since, timestamps=timestamps)
    except AdapterError as exc:
        raise _fail(exc) from exc
    return {"lines": [asdict(line) for line in lines], "count": len(lines)}


@api.get("/endpoints/{endpoint_id}/containers/{ref}/stats")
async def container_stats(
    endpoint_id: int,
    ref: str,
    adapters: AdaptersDep,
    instance: str | None = Query(None, description="Portainer connection id; blank = the default"),
) -> dict[str, Any]:
    adapter = _adapter(adapters, instance)
    try:
        return asdict(await adapter.stats(endpoint_id, ref))
    except AdapterError as exc:
        raise _fail(exc) from exc


# -- page ---------------------------------------------------------------------------------------


@pages.get("/portainer", response_model=None)
async def portainer_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return _templates.TemplateResponse(request, "portainer.html", {"admin": admin})


@pages.get("/portainer/{endpoint_id}/{name}", response_model=None)
async def stack_page(
    endpoint_id: int,
    name: str,
    request: Request,
    session: SessionDep,
    instance: str = Query("", description="Portainer connection id; blank = the default"),
) -> HTMLResponse | RedirectResponse:
    """One stack: its containers (details, logs, actions), all logs, its environment, its
    Portainer settings and its history (the Stack and StackEnv wireframes). Everything about
    the stack is read live by the page; this only hands it which stack."""
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    loose = name == "-"  # the containers of this environment that are in no stack
    initial = {"endpoint": endpoint_id, "name": "" if loose else name, "instance": instance}
    return _templates.TemplateResponse(
        request,
        "stack.html",
        {"admin": admin, "initial": initial, "name": "(no stack)" if loose else name},
    )
