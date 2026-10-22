"""Restore proposals and their approval page.

A restore overwrites a guest with an older copy, so it only ever runs after a
logged-in admin approves it HERE (see pipelines/restore.py). The recovery loop and
the service page create proposals; the link in the alert opens the page.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select

from orchestrator.audit import log as audit
from orchestrator.db.models import PendingApproval, Service
from orchestrator.domain.enums import ApprovalKind, ApprovalStatus
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.restore import (
    RestoreError,
    RestorePipeline,
    approval_url,
    approval_view,
    create_proposal,
    is_open,
)
from orchestrator.web.auth import CurrentAdmin, get_current_admin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

api = APIRouter(prefix="/api", tags=["approvals"], dependencies=[Depends(require_admin)])
pages = APIRouter(tags=["approvals"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
#: Strong references to running restores (an unreferenced task can be collected mid-run).
_RUNNING: set[asyncio.Task[None]] = set()


def _find(session: Session, token: str) -> PendingApproval:
    row = session.exec(
        select(PendingApproval).where(
            PendingApproval.token == token, PendingApproval.kind == ApprovalKind.REVERT
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such restore proposal")
    return row


@pages.get("/approvals/{token}", response_model=None)
async def approval_page(
    token: str, request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse(f"/login?next=/approvals/{token}", status_code=303)
    _find(session, token)
    return _templates.TemplateResponse(request, "approval.html", {"admin": admin, "token": token})


@api.get("/approvals")
async def list_open(session: SessionDep, settings: SettingsDep) -> list[dict[str, Any]]:
    rows = session.exec(
        select(PendingApproval)
        .where(PendingApproval.kind == ApprovalKind.REVERT)
        .order_by(col(PendingApproval.requested_at).desc())
        .limit(30)
    ).all()
    return [approval_view(session, settings, r) for r in rows]


@api.get("/approvals/{token}")
async def get_approval(token: str, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    return approval_view(session, settings, _find(session, token))


@api.post("/approvals/{token}/approve")
async def approve(
    token: str,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    approval = _find(session, token)
    if not is_open(approval):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this proposal is {approval_view(session, settings, approval)['status']}",
        )
    pipeline = RestorePipeline(proxmox=adapters.proxmox, settings=settings)
    try:
        run = pipeline.start(session, approval, admin.username)
    except RestoreError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    task = asyncio.create_task(pipeline.execute(run.id, approval.id, admin.username))  # type: ignore[arg-type]
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return approval_view(session, settings, approval)


@api.post("/approvals/{token}/deny")
async def deny(
    token: str, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    approval = _find(session, token)
    if approval.status is not ApprovalStatus.PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"this proposal is already {approval.status.value}"
        )
    approval.status = ApprovalStatus.DENIED
    approval.approved_by = admin.username
    session.add(approval)
    audit.record(
        session,
        actor=admin.username,
        action="restore.denied",
        target=f"service:{approval.payload.get('service')}",
        details={},
    )
    session.commit()
    return approval_view(session, settings, approval)


@api.post("/services/{slug}/restore-proposal", status_code=status.HTTP_201_CREATED)
async def propose(
    slug: str, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Propose restoring the service from its latest verified backup. Nothing
    happens until the proposal is approved."""
    service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    try:
        approval = create_proposal(
            session, settings, service, requested_by=admin.username, reason="proposed by hand"
        )
    except RestoreError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    session.commit()
    await announce(
        session,
        level="service",
        key=slug,
        name=service.name,
        event="restore_proposed",
        severity="warn",
        title=f"Restore proposed for {service.name}",
        body=f"{admin.username} proposed restoring {approval.payload['snapshot']}. "
        "Nothing happens until it is approved.",
        scope={"slug": slug, "group": service.group},
        approve_url=approval_url(settings, approval.token),
    )
    return approval_view(session, settings, approval)
