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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
from orchestrator.web.auth import get_current_admin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep

api = APIRouter(prefix="/api/portainer", tags=["portainer"], dependencies=[Depends(require_admin)])
pages = APIRouter(tags=["portainer"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

#: How many stack compose files are read at once when labelling image/build.
_KIND_CONCURRENCY = 4


def _adapter(adapters: AdaptersDep) -> PortainerAdapter:
    if adapters.portainer is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Portainer is not set up")
    return adapters.portainer


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
    return f"{label}unreachable, skipped ({reason})"


# -- links the orchestrator keeps (and nothing else about a stack) ------------------------


def _links(session: Session) -> dict[tuple[str, int, str], list[str]]:
    """(kind, environment, name) -> slugs of the sub-services linked to it."""
    out: dict[tuple[str, int, str], list[str]] = {}
    for svc in session.exec(select(Service)).all():
        cfg = svc.controller or {}
        if cfg.get("type") != "portainer":
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
) -> dict[str, Any]:
    data = asdict(s)
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
    return data


def _container_view(
    c: Container, eid: int, base: str, links: dict[tuple[str, int, str], list[str]]
) -> dict[str, Any]:
    data = asdict(c)
    data["endpoint_id"] = eid
    data["open_url"] = container_url(base, eid, c.id)
    data["linked_services"] = links.get(("container", eid, c.name.lower()), [])
    return data


# -- API ----------------------------------------------------------------------------------------


@api.get("/status")
async def portainer_status(adapters: AdaptersDep) -> dict[str, Any]:
    adapter = _adapter(adapters)
    check = await adapter.check()
    return {**asdict(check), "url": adapter.base_url}


@api.get("/environments")
async def environments(adapters: AdaptersDep) -> list[dict[str, Any]]:
    adapter = _adapter(adapters)
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
) -> dict[str, Any]:
    adapter = _adapter(adapters)
    links = _links(session)
    try:
        listing = await adapter.list_stacks(endpoint)
    except AdapterError as exc:
        raise _fail(exc) from exc
    # One environment that cannot be read (an agent that is down, a 502 from it) must not
    # take the others with it: its stacks stay listed, without container counts, and the
    # reason is shown next to the list.
    env_ids = sorted({s.endpoint_id for s in listing.stacks})
    try:
        envs = await adapter.list_environments()
    except AdapterError:
        envs = []
    names = {e.id: e.name for e in envs}
    down = {e.id for e in envs if e.status == "down"}
    for eid in down & set(env_ids):  # Portainer already knows: no point asking it for containers
        listing.errors.setdefault(eid, "Portainer reports it as down")
    env_ids = [e for e in env_ids if e not in down]
    per_env = await asyncio.gather(
        *(adapter.list_containers(e) for e in env_ids), return_exceptions=True
    )
    containers: dict[int, list[Container]] = {}
    unreadable: set[int] = set(down)
    for eid, result in zip(env_ids, per_env, strict=True):
        if isinstance(result, AdapterError):
            unreadable.add(eid)
            listing.errors.setdefault(eid, str(result))
        elif isinstance(result, BaseException):
            raise result
        else:
            containers[eid] = result
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
        rows.append(
            _stack_view(s, base, links, None if s.endpoint_id in unreadable else mine, kind)
        )
    return {
        "stacks": rows,
        "errors": {str(k): _why(names.get(k), v) for k, v in listing.errors.items()},
        "skipped": sorted(listing.errors),
    }


@api.get("/endpoints/{endpoint_id}/stacks/{name}")
async def stack_detail(
    endpoint_id: int, name: str, adapters: AdaptersDep, session: SessionDep
) -> dict[str, Any]:
    adapter = _adapter(adapters)
    links = _links(session)
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
    view = _stack_view(found, base, links, containers, kind)
    view["containers"] = [_container_view(c, endpoint_id, base, links) for c in containers]
    return view


@api.get("/endpoints/{endpoint_id}/containers")
async def containers(
    endpoint_id: int,
    adapters: AdaptersDep,
    session: SessionDep,
    stack: str | None = Query(None),
    all: bool = Query(True, description="Include stopped containers"),
) -> list[dict[str, Any]]:
    adapter = _adapter(adapters)
    links = _links(session)
    try:
        rows = await adapter.list_containers(endpoint_id, stack=stack, include_stopped=all)
    except AdapterError as exc:
        raise _fail(exc) from exc
    return [_container_view(c, endpoint_id, adapter.base_url, links) for c in rows]


@api.get("/endpoints/{endpoint_id}/containers/{ref}")
async def container_detail(
    endpoint_id: int, ref: str, adapters: AdaptersDep, session: SessionDep
) -> dict[str, Any]:
    adapter = _adapter(adapters)
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
        detail.container, endpoint_id, adapter.base_url, _links(session)
    )
    name = detail.container.name
    view["host_commands"] = [
        f"docker logs --tail 200 -f {name}",
        f"docker inspect {name}",
        f"docker restart {name}",
    ]
    return view


@api.get("/endpoints/{endpoint_id}/containers/{ref}/logs")
async def container_logs(
    endpoint_id: int,
    ref: str,
    adapters: AdaptersDep,
    tail: int = Query(200, ge=1, le=2000),
    since: int | None = Query(None, ge=0, description="Unix seconds"),
    timestamps: bool = Query(True),
) -> dict[str, Any]:
    adapter = _adapter(adapters)
    try:
        lines = await adapter.logs(endpoint_id, ref, tail=tail, since=since, timestamps=timestamps)
    except AdapterError as exc:
        raise _fail(exc) from exc
    return {"lines": [asdict(line) for line in lines], "count": len(lines)}


@api.get("/endpoints/{endpoint_id}/containers/{ref}/stats")
async def container_stats(endpoint_id: int, ref: str, adapters: AdaptersDep) -> dict[str, Any]:
    adapter = _adapter(adapters)
    try:
        return asdict(await adapter.stats(endpoint_id, ref))
    except AdapterError as exc:
        raise _fail(exc) from exc


# -- page ---------------------------------------------------------------------------------------


@pages.get("/portainer", response_model=None)
async def portainer_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/portainer", status_code=303)
    return _templates.TemplateResponse(request, "portainer.html", {"admin": admin})
