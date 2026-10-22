"""Start / stop / restart ONE service (or its guest), and read its live status.

Services act through their ``controller:`` (a Portainer container, a Home Assistant
add-on, a systemd unit ...); a root service with none acts on its Proxmox guest.
``target: guest`` always means the (root's) guest. Every action is audited; stopping
or restarting anything in a tree whose root is being backed up is refused.
See docs/controllers.md.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from orchestrator.audit import log as audit
from orchestrator.controllers import (
    ControllerError,
    ControllerUnavailable,
    ServiceController,
    build_controller,
)
from orchestrator.db.models import Service
from orchestrator.registry.tree import TreeIndex
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.routers.dashboard import _backup_currently_running

router = APIRouter(prefix="/api/services", tags=["actions"], dependencies=[Depends(require_admin)])

Action = Literal["start", "stop", "restart"]


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["service", "guest"] = "service"


def _service(session: Session, slug: str) -> Service:
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    return svc


@router.get("/{slug}/controller")
async def get_controller(
    slug: str,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    live: bool = False,
) -> dict[str, Any]:
    """What can act on this service, and (``?live=true``) what each says it is doing."""
    svc = _service(session, slug)
    out: dict[str, Any] = {}
    for target in ("service", "guest"):
        try:
            ctl = build_controller(settings, adapters.proxmox, session, svc, target=target)
        except (ValueError, ControllerError) as exc:
            out[target] = {"available": False, "error": str(exc)}
            continue
        if ctl is None:
            out[target] = {"available": False, "error": None}
            continue
        entry: dict[str, Any] = {"available": True, "describe": ctl.describe(), "type": ctl.type}
        if live:
            try:
                st = await ctl.status()
                entry["status"] = {"running": st.running, "state": st.state, "detail": st.detail}
            except ControllerError as exc:
                entry["status"] = {"running": None, "state": "unknown", "detail": str(exc)}
        out[target] = entry
    return out


async def do_service_action(
    session: Session,
    adapters: Any,
    settings: Any,
    actor: str,
    slug: str,
    action: str,
    *,
    target: str = "service",
) -> str:
    """Run the action; returns the controller's message or raises HTTPException."""
    svc = _service(session, slug)
    ctl: ServiceController | None
    try:
        ctl = build_controller(settings, adapters.proxmox, session, svc, target=target)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if ctl is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{slug!r} has no way to {action} "
            + ("its guest" if target == "guest" else "itself - give it a controller"),
        )
    if action in {"stop", "restart"}:
        root = TreeIndex.build(session).root(svc)
        if root.id is not None and _backup_currently_running(session, root.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"a backup of {root.slug!r} is running - not going to {action} anything under it",
            )
    result = "ok"
    message = ""
    try:
        message = await getattr(ctl, action)()
    except ControllerUnavailable as exc:
        result, message = "unreachable", str(exc)
    except ControllerError as exc:
        result, message = "refused", str(exc)
    audit.record(
        session,
        actor=actor,
        action=f"service.{action}",
        target=f"service:{slug}",
        details={
            "target": target,
            "controller": ctl.describe(),
            "result": result,
            "message": message,
        },
    )
    session.commit()
    if result != "ok":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, message)
    return message


@router.post("/{slug}/actions/{action}")
async def run_action(
    slug: str,
    action: Action,
    body: ActionIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    message = await do_service_action(
        session, adapters, settings, admin.username, slug, action, target=body.target
    )
    return {"slug": slug, "action": action, "target": body.target, "message": message}
