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

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from orchestrator.db.models import (
    BackupPolicy,
    BackupRecord,
    Node,
    ProxyHost,
    Service,
    ServiceState,
)
from orchestrator.domain.enums import BackupMode, GuestKind, RouterProvider
from orchestrator.registry.loader import compute_registry_diff
from orchestrator.registry.probe_specs import PROBE_SPECS
from orchestrator.web.auth import get_current_admin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.routers.activity import build_feed
from orchestrator.web.routers.backup_policies import policy_to_dict
from orchestrator.web.routers.dashboard import service_detail
from orchestrator.web.routers.traffic import build_traffic_view

router = APIRouter(tags=["ui"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


#: Probe state -> the ``ol.steps`` class maintenance.html already styles
#: (ok = check mark, failed/indeterminate = cross).
_STATE_CLASS = {"healthy": "ok", "failed": "failed", "unknown": "indeterminate"}


def _group_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket service rows by their ``group`` field for the dashboard's
    collapsible sections. Named groups sort alphabetically; ungrouped services
    (``group == ""``) always come last, under "Ungrouped" — never hidden."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(row.get("group") or "", []).append(row)
    named = sorted(name for name in buckets if name)
    ordered = named + ([""] if "" in buckets else [])
    return [{"name": name or "Ungrouped", "services": buckets[name]} for name in ordered]


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
                "group": svc.group,
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
    all_nodes = session.exec(select(Node).order_by(Node.name)).all()

    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": admin,
            "services": rows,
            "service_groups": _group_rows(rows),
            "nodes": [{"id": n.id, "name": n.name} for n in all_nodes],
            "guest_kinds": [k.value for k in GuestKind],
            "registry_dirty": diff.dirty,
            "yaml_path": diff.yaml_path,
        },
    )


@router.get("/services/{slug}", response_model=None)
async def service_detail_page(
    slug: str, request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """Edit view for one service and its probes. Server-renders the initial
    state; every change goes through the ``/api/registry/*`` JSON endpoints."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse(f"/login?next=/services/{slug}", status_code=303)

    svc = session.exec(select(Service).where(Service.slug == slug)).first()
    if svc is None:
        raise HTTPException(status_code=404, detail=f"no service {slug!r}")

    nodes = session.exec(select(Node).order_by(Node.name)).all()
    policies = session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
    other_slugs = [
        s for s in session.exec(select(Service.slug).order_by(Service.slug)).all() if s != slug
    ]
    existing_groups = sorted({g for g in session.exec(select(Service.group)).all() if g})
    probe_kinds = [
        {"kind": k.value, "summary": spec.summary, "default_timeout_s": spec.default_timeout_s}
        for k, spec in PROBE_SPECS.items()
    ]
    diff = compute_registry_diff(session)

    return _templates.TemplateResponse(
        request,
        "service_detail.html",
        {
            "admin": admin,
            "service": service_detail(session, svc),
            "nodes": [{"id": n.id, "name": n.name} for n in nodes],
            "policies": [{"id": p.id, "name": p.name} for p in policies],
            "other_slugs": other_slugs,
            "existing_groups": existing_groups,
            "guest_kinds": [k.value for k in GuestKind],
            "probe_kinds": probe_kinds,
            "providers": [p.value for p in RouterProvider],
            "registry_dirty": diff.dirty,
            "yaml_path": diff.yaml_path,
        },
    )


@router.get("/proxy-hosts", response_model=None)
async def proxy_hosts_page(
    request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """List-only proxy-host page. Add/edit live on their own dedicated pages
    (:func:`new_proxy_host_page`/:func:`edit_proxy_host_page`) — this page
    is driven entirely by ``/api/proxy-hosts`` via ``fetch`` for the table,
    plus Resync's bulk-discovery flow (see ``docs/proxy_hosts.md``). The
    service list here is only for the "Discovered on Traefik" section's
    per-row service picker."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/proxy-hosts", status_code=303)

    services = session.exec(select(Service).order_by(Service.slug)).all()
    diff = compute_registry_diff(session)
    return _templates.TemplateResponse(
        request,
        "proxy_hosts.html",
        {
            "admin": admin,
            "services": [{"id": s.id, "slug": s.slug, "name": s.name} for s in services],
            "providers": [p.value for p in RouterProvider],
            "registry_dirty": diff.dirty,
            "yaml_path": diff.yaml_path,
        },
    )


@router.get("/proxy-hosts/new", response_model=None)
async def new_proxy_host_page(
    request: Request, session: SessionDep, service_id: int | None = Query(None)
) -> HTMLResponse | RedirectResponse:
    """Dedicated add page (``docs/roadmap.md``'s "dedicated add/edit pages"
    small feature) — replaces the bottom-of-page form that used to live on
    ``/proxy-hosts`` and the inline one on a service's own page. Submits to
    the existing ``POST /api/proxy-hosts``, now write-through for a
    ``traefik`` host with an upstream (see ``web/routers/proxy_hosts.py``).
    ``?service_id=`` pre-fills the service picker — used by the "Add proxy
    host" link on a service's own detail page.
    """
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/proxy-hosts/new", status_code=303)

    services = session.exec(select(Service).order_by(Service.slug)).all()
    return _templates.TemplateResponse(
        request,
        "proxy_host_form.html",
        {
            "admin": admin,
            "mode": "new",
            "proxy_host": None,
            "initial_service_id": service_id,
            "services": [{"id": s.id, "slug": s.slug, "name": s.name} for s in services],
            "providers": [p.value for p in RouterProvider],
        },
    )


@router.get("/proxy-hosts/{proxy_host_id}/edit", response_model=None)
async def edit_proxy_host_page(
    proxy_host_id: int, request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """Dedicated edit page — see :func:`new_proxy_host_page`. Prefilled
    server-side from the ``ProxyHost`` row directly (same pattern
    ``service_detail_page`` already uses), not re-fetched client-side."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse(f"/login?next=/proxy-hosts/{proxy_host_id}/edit", status_code=303)

    ph = session.get(ProxyHost, proxy_host_id)
    if ph is None:
        raise HTTPException(status_code=404, detail=f"proxy host {proxy_host_id} not found")

    services = session.exec(select(Service).order_by(Service.slug)).all()
    return _templates.TemplateResponse(
        request,
        "proxy_host_form.html",
        {
            "admin": admin,
            "mode": "edit",
            "proxy_host": {
                "id": ph.id,
                "hostname": ph.hostname,
                "upstream": ph.upstream,
                "router_provider": ph.router_provider.value,
                "service_id": ph.service_id,
                "wake_on_hit": ph.wake_on_hit,
                "extra": ph.extra or {},
            },
            "initial_service_id": None,
            "services": [{"id": s.id, "slug": s.slug, "name": s.name} for s in services],
            "providers": [p.value for p in RouterProvider],
        },
    )


@router.get("/backup-policies", response_model=None)
async def backup_policies_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    """List-only backup-policy page, same "dedicated add/edit pages" pattern
    as ``/proxy-hosts`` (:func:`new_backup_policy_page`/
    :func:`edit_backup_policy_page`) — see ``docs/proxy_hosts.md``'s
    "Dedicated add/edit pages" note and ``docs/roadmap.md``. Driven by
    ``/api/backup-policies`` for mutations; the table, per-policy run-history
    expanders and name filter all live here."""
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
            "scheduler_enabled": settings.scheduler_enabled,
            "scheduler_running": bool(scheduler and scheduler.running),
            "dry_run": settings.dry_run,
            "registry_dirty": diff.dirty,
            "yaml_path": diff.yaml_path,
        },
    )


def _policy_form_context(session: Session) -> dict[str, Any]:
    """Shared context for both dedicated backup-policy form pages: the
    service picker (with each service's group, for the group-select-all
    buttons — see ``docs/adding_a_service.md``'s Groups section) and the
    list of modes."""
    services = session.exec(select(Service).order_by(Service.slug)).all()
    return {
        "services": [{"slug": s.slug, "group": s.group} for s in services],
        "groups": sorted({s.group for s in services if s.group}),
        "modes": [m.value for m in BackupMode],
    }


@router.get("/backup-policies/new", response_model=None)
async def new_backup_policy_page(
    request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """Dedicated add page — see :func:`new_proxy_host_page` for the
    precedent this follows. Submits to the existing ``POST
    /api/backup-policies``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/backup-policies/new", status_code=303)

    return _templates.TemplateResponse(
        request,
        "backup_policy_form.html",
        {
            "admin": admin,
            "mode": "new",
            "policy": None,
            **_policy_form_context(session),
        },
    )


@router.get("/backup-policies/{name}/edit", response_model=None)
async def edit_backup_policy_page(
    name: str, request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """Dedicated edit page — see :func:`edit_proxy_host_page` for the
    precedent this follows. Policies are keyed by ``name`` (unique, no
    surrogate id exposed here), so this takes the name directly rather than
    an id, matching ``PUT /api/backup-policies/{name}``. Prefilled from
    :func:`orchestrator.web.routers.backup_policies.policy_to_dict`, the same
    serialization the list page and the API already use."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse(f"/login?next=/backup-policies/{name}/edit", status_code=303)

    policy = session.exec(select(BackupPolicy).where(BackupPolicy.name == name)).one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail=f"no backup policy named {name!r}")

    scheduler = getattr(request.app.state, "scheduler", None)
    return _templates.TemplateResponse(
        request,
        "backup_policy_form.html",
        {
            "admin": admin,
            "mode": "edit",
            "policy": policy_to_dict(session, policy, scheduler),
            **_policy_form_context(session),
        },
    )


@router.get("/traffic", response_model=None)
async def traffic_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    """Per-service / per-host traffic, derived from the samples
    ``orchestrator.scheduler.traffic_collector`` polls out of Traefik's
    Prometheus ``/metrics`` endpoint. See ``docs/traffic_metrics.md``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/traffic", status_code=303)

    traefik_enabled = bool(settings.traefik_api_url.strip())
    view = build_traffic_view(session, traefik_enabled=traefik_enabled)

    return _templates.TemplateResponse(
        request,
        "traffic.html",
        {
            "admin": admin,
            "view": view,
            "poll_interval_s": settings.traefik_metrics_poll_interval_s,
            "retention_days": settings.traefik_metrics_retention_days,
        },
    )


@router.get("/infra", response_model=None)
async def infra_status_page(
    request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """Connectivity check across Proxmox/PBS — the dashboard equivalent of
    ``orchestrator-cli check``. Entirely client-rendered: the page just calls
    the existing ``/api/infra/status`` via ``apiFetch``, same pattern as the
    other list-only pages."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/infra", status_code=303)

    return _templates.TemplateResponse(request, "infra_status.html", {"admin": admin})


@router.get("/infra/guests", response_model=None)
async def infra_guests_page(
    request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """Proxmox guests across the cluster — the dashboard equivalent of
    ``orchestrator-cli guests``. Client-rendered via ``/api/infra/proxmox/guests``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/infra/guests", status_code=303)

    return _templates.TemplateResponse(request, "infra_guests.html", {"admin": admin})


@router.get("/infra/snapshots", response_model=None)
async def infra_snapshots_page(
    request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    """PBS snapshots in the configured datastore — the dashboard equivalent of
    ``orchestrator-cli snapshots``. Client-rendered via ``/api/infra/pbs/snapshots``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/infra/snapshots", status_code=303)

    return _templates.TemplateResponse(request, "infra_snapshots.html", {"admin": admin})


@router.get("/activity", response_model=None)
async def activity_page(
    request: Request,
    session: SessionDep,
    service: str | None = Query(None),
    group: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """History of wakes, backups and admin actions — see ``routers/activity.py``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/activity", status_code=303)

    service = service or None  # the filter form submits "" for "all services"
    group = group or None
    return _templates.TemplateResponse(
        request,
        "activity.html",
        {
            "admin": admin,
            "events": build_feed(session, service=service, group=group),
            "service": service,
            "group": group,
            "slugs": session.exec(select(Service.slug).order_by(Service.slug)).all(),
            "groups": sorted({g for g in session.exec(select(Service.group)).all() if g}),
        },
    )
