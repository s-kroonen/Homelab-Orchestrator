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

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from orchestrator.db.models import BackupRecord, Node, Service, ServiceState
from orchestrator.registry.loader import compute_registry_diff
from orchestrator.web.auth import get_current_admin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.routers.activity import build_feed

router = APIRouter(tags=["ui"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


#: Probe state -> the ``ol.steps`` class maintenance.html already styles
#: (ok = check mark, failed/indeterminate = cross).
_STATE_CLASS = {"healthy": "ok", "failed": "failed", "unknown": "indeterminate"}


def _probe_row(result: dict[str, Any]) -> dict[str, Any]:
    state = str(result.get("state", "unknown"))
    return {
        "name": result.get("probe_name", "?"),
        "kind": result.get("kind", ""),
        "state": state,
        "css": _STATE_CLASS.get(state, ""),
        "message": result.get("message") or "",
        "latency_ms": result.get("latency_ms"),
    }


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
                # The "why" behind the badge: each probe's own state + message.
                "probes": [_probe_row(p) for p in (state.last_probe_results if state else [])],
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


@router.get("/activity", response_model=None)
async def activity_page(
    request: Request,
    session: SessionDep,
    service: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """History of wakes, backups and admin actions — see ``routers/activity.py``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/activity", status_code=303)

    service = service or None  # the filter form submits "" for "all services"
    return _templates.TemplateResponse(
        request,
        "activity.html",
        {
            "admin": admin,
            "events": build_feed(session, service=service),
            "service": service,
            "slugs": session.exec(select(Service.slug).order_by(Service.slug)).all(),
        },
    )
