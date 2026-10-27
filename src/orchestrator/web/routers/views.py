"""The list pages of the new layout (docs/design/README.md): Services, Nodes & guests, Admins &
devices.

Each page is rendered with its data already in it (``initial``), so the first paint needs no
request; ``GET /api/views/<page>`` returns the same data for the page's live refresh. Both read the
database only - what the scanners last saw - and never wait on Proxmox, PBS or Traefik Manager.
Live readings that do (node power, the guest list) are separate calls the page makes after it is
shown. Every change goes through the existing ``/api/registry/*``, ``/api/bulk/*`` and action
endpoints.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlmodel import Session, select

from orchestrator.db.models import (
    BackupPolicy,
    BackupRecord,
    Node,
    PipelineRun,
    Service,
    ServiceState,
)
from orchestrator.domain.enums import GuestKind, PipelineKind, PipelineStatus
from orchestrator.registry.tree import TreeIndex
from orchestrator.web.auth import get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import SessionDep
from orchestrator.web.routers.ui import _down_label, _guest_cell_for, _service_choices
from orchestrator.web.templating import templates

pages = APIRouter(tags=["views"])
api = APIRouter(prefix="/api/views", tags=["views"], dependencies=[Depends(require_admin)])
router = APIRouter()
router.include_router(pages)
router.include_router(api)


# --- services ---------------------------------------------------------------------------------


def _latest_backup_runs(session: Session) -> dict[int, PipelineRun]:
    """The newest backup run per service."""
    newest = (
        select(func.max(PipelineRun.id))
        .where(PipelineRun.kind == PipelineKind.BACKUP, PipelineRun.service_id.is_not(None))  # type: ignore[union-attr]
        .group_by(PipelineRun.service_id)
    )
    runs = session.exec(select(PipelineRun).where(PipelineRun.id.in_(newest))).all()  # type: ignore[union-attr]
    return {r.service_id: r for r in runs if r.service_id is not None}


def _state(svc: Service, state: ServiceState | None, backup_failed: bool) -> dict[str, str]:
    """One status word and its class (ok warn fail off pending), and why."""
    if not svc.enabled:
        return {
            "cls": "off",
            "label": "disabled",
            "why": "switched off: not checked, woken or backed up",
        }
    verdict = state.last_verdict.value if state else "unknown"
    reason = (state.notes or "") if state else ""
    if verdict == "healthy":
        if backup_failed:
            return {
                "cls": "warn",
                "label": "backup failed",
                "why": "healthy, but its last backup failed",
            }
        return {"cls": "ok", "label": "healthy", "why": reason}
    if verdict == "failed":
        return {"cls": "fail", "label": "failed", "why": reason}
    if verdict == "down":
        if _down_label(state) == "node off":
            return {"cls": "off", "label": "asleep", "why": "asleep: its node is off"}
        return {"cls": "off", "label": "stopped", "why": "its guest is stopped"}
    return {"cls": "off", "label": "unknown", "why": reason or "not checked yet"}


def _last_check(state: ServiceState | None) -> dict[str, Any]:
    if state is None or state.last_verdict_at is None:
        return {"text": "not checked yet", "at": None}
    probes = [
        p
        for p in state.last_probe_results
        if p.get("kind") != "guest" and not (p.get("details") or {}).get("skipped")
    ]
    at = state.last_verdict_at.isoformat()
    if state.last_verdict.value == "down":
        return {"text": "not checked while off", "at": at}
    bad = [p for p in probes if p.get("state") != "healthy"]
    if bad:
        p = bad[0]
        msg = (p.get("message") or p.get("state") or "").strip()
        return {"text": f"{p.get('probe_name', '?')}: {msg}"[:90], "at": at}
    if not probes:
        return {"text": "guest only", "at": at}
    return {"text": f"{len(probes)}/{len(probes)} probes ok", "at": at}


def service_rows(session: Session) -> list[dict[str, Any]]:
    nodes = {n.id: n for n in session.exec(select(Node)).all()}
    states = {s.service_id: s for s in session.exec(select(ServiceState)).all()}
    policies = {p.id: p.name for p in session.exec(select(BackupPolicy)).all()}
    services = list(session.exec(select(Service).order_by(Service.slug)).all())
    tree = TreeIndex(services)
    runs = _latest_backup_runs(session)
    guest_cells: dict[str, dict[str, str]] = {}
    rows: list[dict[str, Any]] = []
    for svc, depth in tree.walk():
        state = states.get(svc.id) if svc.id is not None else None
        parent = tree.parent(svc)
        root = tree.root(svc)
        node = nodes.get(root.node_id) if root.node_id is not None else None
        run = runs.get(svc.id) if svc.id is not None else None
        last_backup = (
            session.get(BackupRecord, state.last_backup_id)
            if state is not None and state.last_backup_id is not None
            else None
        )
        backup_failed = run is not None and run.status is PipelineStatus.FAILED
        guest = _guest_cell_for(svc, state, tree, states, guest_cells)
        if parent is not None:
            runs_on = {
                "main": parent.name or parent.slug,
                "sub": "sub-service",
                "href": f"/services/{parent.slug}",
            }
        elif svc.guest_kind is GuestKind.NONE:
            runs_on = {"main": node.name if node else "—", "sub": "no guest", "href": None}
        else:
            runs_on = {
                "main": node.name if node else "—",
                "sub": f"{svc.guest_kind.value} {svc.guest_id} · {guest['label']}",
                "href": None,
            }
        if svc.backup_excluded:
            backup = {"main": "excluded", "sub": svc.backup_excluded_reason, "cls": "off"}
        elif svc.backup_policy_id is None and parent is not None:
            backup = {"main": "with parent", "sub": "", "cls": "off"}
        elif svc.backup_policy_id is None:
            backup = {"main": "no policy", "sub": "", "cls": "warn"}
        elif backup_failed and run is not None:
            backup = {
                "main": policies.get(svc.backup_policy_id, "?"),
                "sub": "failed",
                "at": (run.finished_at or run.started_at).isoformat(),
                "cls": "warn",
            }
        elif last_backup is not None:
            backup = {
                "main": policies.get(svc.backup_policy_id, "?"),
                "sub": "ok" if last_backup.verified else "not verified",
                "at": last_backup.created_at.isoformat(),
                "cls": "ok",
            }
        else:
            backup = {
                "main": policies.get(svc.backup_policy_id, "?"),
                "sub": "never backed up",
                "cls": "warn",
            }
        node_wakes = node.wake_on_hit_enabled if node is not None else False
        if parent is not None and not svc.wake_on_hit_enabled:
            wake = "via parent"
        else:
            wake = "yes" if svc.wake_on_hit_enabled and node_wakes else "no"
        rows.append(
            {
                "slug": svc.slug,
                "name": svc.name or svc.slug,
                "group": svc.group,
                "enabled": svc.enabled,
                "parent": parent.slug if parent else None,
                "depth": depth,
                "node": node.name if node else "",
                "node_id": root.node_id,
                "guest_kind": svc.guest_kind.value,
                "guest_id": svc.guest_id,
                "state": _state(svc, state, backup_failed),
                "check": _last_check(state),
                "runs_on": runs_on,
                "backup": backup,
                "backup_policy_id": svc.backup_policy_id,
                "not_backed_up": parent is None
                and not svc.backup_excluded
                and (
                    svc.backup_policy_id is None
                    or (last_backup is None and run is None)
                    or backup_failed
                ),
                "wake_on_hit": wake,
                "wake_on_hit_enabled": svc.wake_on_hit_enabled,
                "has_controller": bool(svc.controller) or svc.guest_kind is not GuestKind.NONE,
            }
        )
    return rows


def services_view(session: Session) -> dict[str, Any]:
    return {
        "rows": service_rows(session),
        "nodes": [
            {"id": n.id, "name": n.name}
            for n in session.exec(select(Node).order_by(Node.name)).all()
        ],
        "policies": [
            {"id": p.id, "name": p.name}
            for p in session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
        ],
        "groups": sorted({g for g in session.exec(select(Service.group)).all() if g}),
        "guest_kinds": [k.value for k in GuestKind],
    }


@api.get("/services")
async def services_api(session: SessionDep) -> dict[str, Any]:
    return services_view(session)


@pages.get("/services", response_model=None)
async def services_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return templates.TemplateResponse(
        request, "services.html", {"admin": admin, "initial": services_view(session)}
    )


# --- nodes ------------------------------------------------------------------------------------


def nodes_view(session: Session) -> dict[str, Any]:
    services = list(session.exec(select(Service)).all())
    tree = TreeIndex(services)
    out = []
    for n in session.exec(select(Node).order_by(Node.name)).all():
        mine = [s for s in services if tree.root(s).node_id == n.id]
        out.append(
            {
                "id": n.id,
                "name": n.name,
                "always_on": n.always_on,
                "address": n.address,
                "power_mgr_target": n.power_mgr_target,
                "notes": n.notes,
                "wake_on_hit_enabled": n.wake_on_hit_enabled,
                "services": len(mine),
                "service_slugs": sorted(s.slug for s in mine if tree.parent(s) is None),
            }
        )
    return {"nodes": out, "services": [{"slug": s.slug, "name": s.name} for s in services]}


@api.get("/nodes")
async def nodes_api(session: SessionDep) -> dict[str, Any]:
    return nodes_view(session)


@pages.get("/nodes", response_model=None)
async def nodes_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return templates.TemplateResponse(
        request,
        "nodes.html",
        {
            "admin": admin,
            "initial": nodes_view(session),
            "services_initial": services_view(session),
        },
    )


# --- admins & devices -------------------------------------------------------------------------


@pages.get("/admins", response_model=None)
async def admins_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return templates.TemplateResponse(request, "admins.html", {"admin": admin})


@api.get("/proxy-host-services")
async def proxy_host_services(session: SessionDep) -> list[dict[str, Any]]:
    """The proxy host drawer's service picker, each service with its address ("Use it")."""
    return _service_choices(session)
