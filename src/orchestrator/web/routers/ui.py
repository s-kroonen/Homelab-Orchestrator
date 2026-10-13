"""The dashboard HTML page itself — phase 7's "something to show."

A thin view over data the JSON API (``dashboard.py``, `backup.py``) already
exposes: every service, its node, its last health verdict, and its last
backup. The page's buttons call that existing JSON API via ``fetch`` from
``static/dashboard.js`` rather than this router reimplementing those
actions.

This is an HTML page, not a JSON endpoint, so unlike the ``/api/*`` routers
it does NOT use the ``require_admin`` dependency (which 401s) — an
unauthenticated visitor should be redirected to ``/login``, not shown a raw
401 body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from orchestrator.db.models import BackupPolicy, BackupRecord, Node, Service, ServiceState
from orchestrator.domain.enums import BackupMode
from orchestrator.registry.loader import compute_registry_diff
from orchestrator.web.auth import get_current_admin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.routers.backup_policies import policy_to_dict

router = APIRouter(tags=["ui"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_model=None)
async def dashboard_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/", status_code=303)

    nodes = {n.id: n.name for n in session.exec(select(Node)).all()}
    states = {s.service_id: s for s in session.exec(select(ServiceState)).all()}
    services = session.exec(select(Service).order_by(Service.slug)).all()

    rows: list[dict[str, Any]] = []
    for svc in services:
        state = states.get(svc.id) if svc.id is not None else None
        last_backup = None
        if state is not None and state.last_backup_id is not None:
            last_backup = session.get(BackupRecord, state.last_backup_id)
        rows.append(
            {
                "slug": svc.slug,
                "name": svc.name,
                "enabled": svc.enabled,
                "node": nodes.get(svc.node_id, "?"),
                "verdict": state.last_verdict.value if state else "unknown",
                "verdict_at": (
                    state.last_verdict_at.isoformat() if state and state.last_verdict_at else None
                ),
                "last_backup_at": last_backup.created_at.isoformat() if last_backup else None,
                "last_backup_verified": last_backup.verified if last_backup else None,
            }
        )

    diff = compute_registry_diff(session)

    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": admin,
            "services": rows,
            "registry_dirty": diff.dirty,
            "yaml_path": diff.yaml_path,
        },
    )


@router.get("/backup-policies", response_model=None)
async def backup_policies_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    """Phase 6: list/create/edit/delete backup policies. Same data and same
    mutations as ``/api/backup-policies``; the page's forms call that API."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/backup-policies", status_code=303)

    scheduler = getattr(request.app.state, "scheduler", None)
    policies = session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
    diff = compute_registry_diff(session)

    return _templates.TemplateResponse(
        request,
        "backup_policies.html",
        {
            "admin": admin,
            "policies": [policy_to_dict(session, p, scheduler) for p in policies],
            "services": [s.slug for s in session.exec(select(Service).order_by(Service.slug))],
            "modes": [m.value for m in BackupMode],
            "scheduler_enabled": settings.scheduler_enabled,
            "scheduler_running": bool(scheduler and scheduler.running),
            "dry_run": settings.dry_run,
            "registry_dirty": diff.dirty,
            "yaml_path": diff.yaml_path,
        },
    )
