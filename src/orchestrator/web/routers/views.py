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

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlmodel import Session, col, select

from orchestrator.db.models import (
    BackupPolicy,
    BackupRecord,
    Node,
    PipelineRun,
    ProxyHost,
    Service,
    ServiceState,
)
from orchestrator.domain.enums import BackupMode, GuestKind, PipelineKind, PipelineStatus
from orchestrator.registry.probe_specs import PROBE_SPECS
from orchestrator.registry.tree import TreeIndex
from orchestrator.web.auth import get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.routers.backup_policies import policy_to_dict
from orchestrator.web.routers.dashboard import service_detail
from orchestrator.web.routers.ui import (
    _down_label,
    _guest_cell_for,
    _ping_transport,
    _probe_row,
    _requirable_services,
    _service_choices,
    guest_gate_view,
)
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
        wake_why = ""
        if parent is not None and not svc.wake_on_hit_enabled:
            wake = "via parent"
        elif svc.wake_on_hit_enabled and node_wakes:
            wake = "yes (button)" if svc.wake_trigger.value == "button" else "yes"
        else:
            wake = "no"
            wake_why = (
                "off for this service"
                if not svc.wake_on_hit_enabled
                else f"off for its node {node.name if node else '?'}"
            )
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
                "wake_on_hit_why": wake_why,
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


# --- one service ------------------------------------------------------------------------------


def health_view(session: Session, svc: Service) -> dict[str, Any]:
    """The last scan of one service: verdict, why, the guest gate and each probe's result."""
    state = session.get(ServiceState, svc.id) if svc.id is not None else None
    results = state.last_probe_results if state else []
    return {
        "verdict": state.last_verdict.value if state else "unknown",
        "verdict_at": (
            state.last_verdict_at.isoformat() if state and state.last_verdict_at else None
        ),
        "reason": (state.notes or "") if state else "",
        "down_label": _down_label(state),
        "guest": guest_gate_view(state),
        "probe_results": {
            str(r.get("probe_name")): _probe_row(r) for r in results if r.get("kind") != "guest"
        },
    }


def service_view(session: Session, settings: Any, svc: Service) -> dict[str, Any]:
    """Everything the service page shows, from the database only."""
    detail = service_detail(session, svc)
    row = next((r for r in service_rows(session) if r["slug"] == svc.slug), None)
    tree = TreeIndex.build(session)
    below = {d.slug for d in tree.descendants(svc)}
    others = [s.slug for s in session.exec(select(Service).order_by(Service.slug)).all()]
    policy = (
        session.get(BackupPolicy, svc.backup_policy_id)
        if svc.backup_policy_id is not None
        else None
    )
    return {
        "service": detail,
        "row": row,
        "health": health_view(session, svc),
        "policy": (
            {
                "name": policy.name,
                "schedule_cron": policy.schedule_cron,
                "retention": policy.retention,
            }
            if policy
            else None
        ),
        "other_slugs": [x for x in others if x != svc.slug],
        "parent_choices": [x for x in others if x != svc.slug and x not in below],
        "requirable": _requirable_services(session, exclude=svc.slug),
        "probe_kinds": [
            {"kind": k.value, "summary": spec.summary, "default_timeout_s": spec.default_timeout_s}
            for k, spec in PROBE_SPECS.items()
        ],
        "ping_transport": _ping_transport(settings.ip_discovery_transport),
        **{k: v for k, v in services_view(session).items() if k != "rows"},
    }


def _service_or_404(session: Session, slug: str) -> Service:
    svc = session.exec(select(Service).where(Service.slug == slug)).first()
    if svc is None:
        raise HTTPException(status_code=404, detail=f"no service {slug!r}")
    return svc


@api.get("/service/{slug}")
async def service_api(slug: str, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    return service_view(session, settings, _service_or_404(session, slug))


@pages.get("/services/{slug}", response_model=None)
async def service_page(
    slug: str, request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    svc = _service_or_404(session, slug)
    return templates.TemplateResponse(
        request,
        "service_detail.html",
        {"admin": admin, "service": svc, "initial": service_view(session, settings, svc)},
    )


# --- backups ----------------------------------------------------------------------------------


def _last_runs(session: Session, hours: int = 24) -> list[dict[str, Any]]:
    """The newest backup run of each service in the last ``hours``, with what it produced."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    runs = session.exec(
        select(PipelineRun)
        .where(PipelineRun.kind == PipelineKind.BACKUP, col(PipelineRun.started_at) >= since)
        .order_by(col(PipelineRun.started_at).desc())
    ).all()
    slugs = {s.id: s.slug for s in session.exec(select(Service)).all()}
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for run in runs:
        if run.service_id is None or run.service_id in seen or run.service_id not in slugs:
            continue
        seen.add(run.service_id)
        rec = session.exec(
            select(BackupRecord).where(BackupRecord.pipeline_run_id == run.id)
        ).first()
        took = (
            (run.finished_at - run.started_at).total_seconds()
            if run.finished_at and run.started_at
            else None
        )
        woke = [
            str((st.get("detail") or {}).get("node") or st.get("name"))
            for st in run.steps or []
            if (st.get("detail") or {}).get("woke")
        ]
        out.append(
            {
                "run_id": run.id,
                "service": slugs[run.service_id],
                "status": run.status.value,
                "error": run.error,
                "started_at": run.started_at.isoformat(),
                "took_s": took,
                "size_bytes": rec.size_bytes if rec else None,
                "verified": rec.verified if rec else None,
                "woke": woke,
            }
        )
    return out


def backups_view(session: Session, scheduler: Any) -> dict[str, Any]:
    policies = [
        policy_to_dict(session, p, scheduler)
        for p in session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
    ]
    services = list(session.exec(select(Service).order_by(Service.slug)).all())
    tree = TreeIndex(services)
    return {
        "policies": policies,
        "last_runs": _last_runs(session),
        "excluded": [
            {"slug": s.slug, "reason": s.backup_excluded_reason}
            for s in services
            if s.backup_excluded
        ],
        "unprotected": [
            s.slug
            for s in services
            if tree.parent(s) is None and not s.backup_excluded and s.backup_policy_id is None
        ],
        "services": [
            {
                "slug": s.slug,
                "group": s.group,
                "excluded": s.backup_excluded,
                "reason": s.backup_excluded_reason,
            }
            for s in services
        ],
        "requirable": _requirable_services(session),
        "groups": sorted({s.group for s in services if s.group}),
        "modes": [m.value for m in BackupMode],
        "scheduler_running": bool(scheduler and scheduler.running),
    }


@api.get("/backups")
async def backups_api(request: Request, session: SessionDep) -> dict[str, Any]:
    return backups_view(session, getattr(request.app.state, "scheduler", None))


@pages.get("/backup-policies", response_model=None)
async def backups_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "admin": admin,
            "initial": backups_view(session, getattr(request.app.state, "scheduler", None)),
        },
    )


@pages.get("/backup-policies/new", response_model=None)
async def new_policy_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    """Old address of the add form: opens the drawer on the Backups page."""
    if get_current_admin(request, session) is None:
        return login_redirect(request)
    return RedirectResponse("/backup-policies?new=1", status_code=303)


@pages.get("/backup-policies/{name}/edit", response_model=None)
async def edit_policy_page(
    name: str, request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """Old address of the edit form: opens the drawer on the Backups page."""
    if get_current_admin(request, session) is None:
        return login_redirect(request)
    if session.exec(select(BackupPolicy).where(BackupPolicy.name == name)).first() is None:
        raise HTTPException(status_code=404, detail=f"backup policy {name!r} not found")
    from urllib.parse import quote

    return RedirectResponse(f"/backup-policies?edit={quote(name)}", status_code=303)


@pages.get("/infra/snapshots", response_model=None)
async def snapshots_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    """PBS snapshots are a tab of Backups now."""
    if get_current_admin(request, session) is None:
        return login_redirect(request)
    return RedirectResponse("/backup-policies?tab=snapshots", status_code=303)


# --- dashboard ---------------------------------------------------------------------------------


def _attention(session: Session, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What needs a look, worst first. Only from the database; the page adds what needs a live
    read (proxy hosts changed in Traefik) once it is shown."""
    items: list[dict[str, Any]] = []
    runs = _latest_backup_runs(session)
    ids = {s.slug: s.id for s in session.exec(select(Service)).all()}
    for r in rows:
        st = r["state"]
        if st["cls"] == "fail":
            items.append(
                {
                    "kind": "failed",
                    "cls": "fail",
                    "label": "Failed",
                    "slug": r["slug"],
                    "title": f"{r['name']} is down",
                    "text": r["check"]["text"] if r["check"]["text"] else st["why"],
                    "at": r["check"]["at"],
                    "action": "check",
                }
            )
        elif st["label"] == "backup failed":
            run = runs.get(ids.get(r["slug"]) or -1)
            items.append(
                {
                    "kind": "backup",
                    "cls": "warn",
                    "label": "Backup",
                    "slug": r["slug"],
                    "title": f"{r['name']} backup failed",
                    "text": (run.error if run and run.error else "its last backup did not finish"),
                    "at": (run.finished_at or run.started_at).isoformat() if run else None,
                    "action": "run",
                }
            )
    return items


def dashboard_view(session: Session, scheduler: Any) -> dict[str, Any]:
    rows = service_rows(session)
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["parent"]:
            continue
        groups.setdefault(r["group"] or r["node"] or "Other", []).append(
            {
                "slug": r["slug"],
                "name": r["name"],
                "cls": r["state"]["cls"],
                "label": r["state"]["label"],
            }
        )
    policies = [
        policy_to_dict(session, p, scheduler)
        for p in session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
    ]
    nxt = sorted((p for p in policies if p["next_run"]), key=lambda p: p["next_run"])
    last = _last_runs(session)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]["cls"]] = counts.get(r["state"]["cls"], 0) + 1
    return {
        "attention": _attention(session, rows),
        "services": {"total": len(rows), "by_state": counts},
        "glance": [{"name": k, "services": v} for k, v in sorted(groups.items())],
        "nodes": nodes_view(session)["nodes"],
        "proxy_hosts": len(session.exec(select(ProxyHost)).all()),
        "backups": {
            "runs": len(last),
            "ok": sum(1 for x in last if x["status"] == "succeeded"),
            "next_run": nxt[0]["next_run"] if nxt else None,
            "next_policy": nxt[0]["name"] if nxt else None,
        },
        "view": services_view(session) | {"rows": rows},
    }


@api.get("/dashboard")
async def dashboard_api(request: Request, session: SessionDep) -> dict[str, Any]:
    return dashboard_view(session, getattr(request.app.state, "scheduler", None))


@pages.get("/", response_model=None)
async def dashboard_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    """What needs attention first, then the numbers, the nodes, recent activity and every
    service at a glance (the Main wireframe)."""
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": admin,
            "initial": dashboard_view(session, getattr(request.app.state, "scheduler", None)),
        },
    )
