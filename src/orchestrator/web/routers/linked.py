"""What is linked to a service, whether it is really on, and the one switch.

Everything is read from the systems themselves (Traefik Manager, Portainer, Proxmox);
the registry only keeps what was asked for. See docs/portainer_deploys.md.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from orchestrator.db.models import Service
from orchestrator.registry.linked import gather_context, read_linked
from orchestrator.registry.switch import SwitchRefused, switch_service
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api/services", tags=["linked"], dependencies=[Depends(require_admin)])


class SwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["on", "off"]
    #: Off only: also stop the Proxmox guest (a root service). Asked every time.
    include_guest: bool = False
    #: On only: also start the children that were on before, instead of just watching them.
    restore_children: bool = False


def _service(session: SessionDep, slug: str) -> Service:
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    return svc


@router.get("/{slug}/linked")
async def linked_state(
    slug: str, session: SessionDep, adapters: AdaptersDep, settings: SettingsDep
) -> dict[str, Any]:
    """The service's linked parts (proxy hosts, stack / container, guest), each as it is
    right now, and whether they agree."""
    svc = _service(session, slug)
    return (await read_linked(session, adapters, settings, svc)).as_dict()


@router.get("/linked/all")
async def linked_all(
    session: SessionDep, adapters: AdaptersDep, settings: SettingsDep
) -> dict[str, Any]:
    """Every service that has something linked: ``{slug: state}`` (for the map and lists)."""
    ctx = await gather_context(session, adapters)
    out: dict[str, Any] = {}
    for svc in session.exec(select(Service).order_by(Service.slug)).all():
        state = await read_linked(session, adapters, settings, svc, ctx)
        if state.parts:
            out[svc.slug] = state.as_dict()
    return out


@router.post("/{slug}/switch")
async def switch(
    slug: str,
    body: SwitchIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    """Switch the service and everything linked to it on or off (see the module docstring
    of ``orchestrator.registry.switch`` for the order). Also the way to make linked parts
    that disagree agree again: switching to the state most of them are in is idempotent."""
    try:
        result = await switch_service(
            session,
            adapters,
            settings,
            slug,
            body.state == "on",
            actor=admin.username,
            include_guest=body.include_guest,
            restore_children=body.restore_children,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except SwitchRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return result.as_dict()
