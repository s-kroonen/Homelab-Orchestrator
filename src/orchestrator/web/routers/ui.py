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

from orchestrator.adapters.proxy.base import RouteOptions
from orchestrator.db.models import (
    BackupPolicy,
    BackupRecord,
    Node,
    ProxyHost,
    Service,
    ServiceState,
)
from orchestrator.domain.enums import BackupMode, GuestKind, RouterProvider
from orchestrator.ipdiscovery.discovery import SOURCE_LABELS, effective_ip, transport_block
from orchestrator.pipelines.wake import wake_is_unsupported
from orchestrator.registry.backup_plan import BLOCKER_NO_POWER_TARGET
from orchestrator.registry.loader import compute_registry_diff
from orchestrator.registry.probe_specs import PROBE_SPECS
from orchestrator.registry.tree import TreeIndex
from orchestrator.web.auth import get_current_admin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.routers.activity import KINDS, build_feed, filter_options
from orchestrator.web.routers.backup_policies import policy_to_dict
from orchestrator.web.routers.dashboard import service_detail
from orchestrator.web.routers.geo_traffic import build_geo_view
from orchestrator.web.routers.traffic import (
    DEFAULT_RANGE,
    RANGES,
    collector_status_payload,
)

router = APIRouter(tags=["ui"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


#: Probe state -> the ``ol.steps`` class maintenance.html already styles
#: (ok = check mark, failed/indeterminate = cross).
_STATE_CLASS = {
    "healthy": "ok",
    "failed": "failed",
    "unknown": "indeterminate",
    "down": "indeterminate",
}


def _service_choices(session: Session) -> list[dict[str, Any]]:
    """Services for the proxy-host form's picker, each with its effective IP
    (for "Use service IP")."""
    states = {s.service_id: s for s in session.exec(select(ServiceState)).all()}
    out: list[dict[str, Any]] = []
    for s in session.exec(select(Service).order_by(Service.slug)).all():
        ip, source = effective_ip(s, states.get(s.id))
        out.append(
            {
                "id": s.id,
                "slug": s.slug,
                "name": s.name,
                "ip": ip,
                "ip_source": SOURCE_LABELS.get(source, source),
            }
        )
    return out


def _requirable_services(session: Session, *, exclude: str | None = None) -> list[dict[str, Any]]:
    """Services an operator can pick as a backup prerequisite, each with its node
    and — when its node can never be woken (no power target, not always on) — the
    blocker, so the pickers can warn before a backup fails on it."""
    nodes = {n.id: n for n in session.exec(select(Node)).all()}
    out: list[dict[str, Any]] = []
    for svc in session.exec(select(Service).order_by(Service.slug)).all():
        if svc.slug == exclude:
            continue
        node = nodes.get(svc.node_id) if svc.node_id is not None else None
        out.append(
            {
                "slug": svc.slug,
                "name": svc.name,
                "group": svc.group,
                "node": node.name if node else "",
                "blocker": BLOCKER_NO_POWER_TARGET if wake_is_unsupported(node) else None,
            }
        )
    return out


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
    # Probes the guest gate stopped from running are not indeterminate —
    # they simply did not run. No ✓/✗ for them.
    skipped = bool((result.get("details") or {}).get("skipped"))
    return {
        "name": result.get("probe_name", "?"),
        "kind": result.get("kind", ""),
        "state": "skipped" if skipped else state,
        "css": "skipped" if skipped else _STATE_CLASS.get(state, ""),
        "message": result.get("message") or "",
        "latency_ms": result.get("latency_ms"),
    }


def _down_label(state: ServiceState | None) -> str:
    """Badge text for a DOWN verdict: "node off" or "stopped", read from the
    guest-gate result the engine records first."""
    for result in (state.last_probe_results if state else [])[:1]:
        if (
            result.get("kind") == "guest"
            and (result.get("details") or {}).get("node_online") is False
        ):
            return "node off"
    return "stopped"


def guest_gate_view(state: ServiceState | None) -> dict[str, Any] | None:
    """The guest-gate result from the last scan, shaped for the service page:
    node state + guest state. None when the last scan had no guest gate."""
    results = state.last_probe_results if state else []
    first = results[0] if results else None
    if not first or first.get("kind") != "guest":
        return None
    details = first.get("details") or {}
    node_online = details.get("node_online")
    guest_status = details.get("guest_status")
    return {
        "state": str(first.get("state", "unknown")),
        "message": first.get("message") or "",
        "node": details.get("node") or "?",
        "node_state": (
            "online" if node_online is True else "offline" if node_online is False else "unknown"
        ),
        "guest": f"{details.get('guest_kind') or '?'} {details.get('guest_id') or '?'}",
        "guest_state": guest_status or ("not checked" if node_online is False else "unknown"),
    }


def _guest_cell_for(
    svc: Service,
    state: ServiceState | None,
    tree: TreeIndex,
    states: dict[int, ServiceState],
    cache: dict[str, dict[str, str]],
) -> dict[str, str]:
    """The guest cell; a sub-service shows its root's guest, noted as inherited."""
    root = tree.root(svc)
    if root is svc:
        cell = _guest_cell(svc, state)
        cache[svc.slug] = cell
        return cell
    base = cache.get(root.slug) or _guest_cell(
        root, states.get(root.id) if root.id is not None else None
    )
    return {**base, "title": f"{root.slug}'s guest - {base['title']}"}


def _children_summary(
    tree: TreeIndex, svc: Service, states: dict[int, ServiceState]
) -> dict[str, Any] | None:
    """How the sub-services below this one are doing, for the parent's row."""
    kids = tree.descendants(svc)
    if not kids:
        return None
    counts: dict[str, int] = {}
    for k in kids:
        if not k.enabled:
            key = "disabled"
        else:
            st = states.get(k.id) if k.id is not None else None
            key = st.last_verdict.value if st else "unknown"
        counts[key] = counts.get(key, 0) + 1
    worst = next(
        (k for k in ("failed", "down", "unknown", "healthy", "disabled") if counts.get(k)),
        "unknown",
    )
    return {"total": len(kids), "counts": counts, "worst": worst}


def _guest_cell(svc: Service, state: ServiceState | None) -> dict[str, str]:
    """What the services table shows for the Proxmox guest: its power state as
    of the last health scan (``css`` is a badge class)."""
    if svc.guest_kind is GuestKind.NONE:
        return {"label": "—", "css": "", "title": "no Proxmox guest"}
    gate = guest_gate_view(state)
    if gate is None:
        return {"label": "unknown", "css": "unknown", "title": "not scanned yet"}
    if gate["node_state"] == "offline":
        return {"label": "node off", "css": "down", "title": gate["message"]}
    status = gate["guest_state"]
    css = {"running": "healthy", "stopped": "disabled"}.get(status, "unknown")
    return {"label": status, "css": css, "title": f"{gate['guest']} - {gate['message']}"}


@router.get("/", response_model=None)
async def dashboard_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/", status_code=303)

    node_rows = {n.id: n for n in session.exec(select(Node)).all()}
    nodes = {nid: n.name for nid, n in node_rows.items()}
    states = {s.service_id: s for s in session.exec(select(ServiceState)).all()}
    services = session.exec(select(Service).order_by(Service.slug)).all()
    tree = TreeIndex(list(services))
    # Parents before their children, so a group lists them as a tree.
    services = [s for s, _ in tree.walk()]
    guest_cells: dict[str, dict[str, str]] = {}

    rows: list[dict[str, Any]] = []
    for svc in services:
        state = states.get(svc.id) if svc.id is not None else None
        last_backup = None
        if state is not None and state.last_backup_id is not None:
            last_backup = session.get(BackupRecord, state.last_backup_id)
        ip, ip_source = effective_ip(svc, state)
        node = node_rows.get(svc.node_id)
        node_wakes = node.wake_on_hit_enabled if node is not None else False
        rows.append(
            {
                "slug": svc.slug,
                "name": svc.name,
                "ip": ip,
                "ip_source": SOURCE_LABELS.get(ip_source, ip_source),
                "enabled": svc.enabled,
                "group": svc.group,
                "node": nodes.get(svc.node_id, "?"),
                "guest": _guest_cell_for(svc, state, tree, states, guest_cells),
                "parent": tree.parent(svc).slug if tree.parent(svc) else None,
                "depth": tree.depth(svc),
                "children": _children_summary(tree, svc, states),
                "served": tree.is_served(svc),
                # On only when the service AND its node allow it (see Node.wake_on_hit_enabled).
                "wake_on_hit": svc.wake_on_hit_enabled and node_wakes,
                "wake_on_hit_why": (
                    ""
                    if svc.wake_on_hit_enabled and node_wakes
                    else (
                        "off for this service"
                        if not svc.wake_on_hit_enabled
                        else f"off for its node {nodes.get(svc.node_id, '?')}"
                    )
                ),
                "wake_trigger": svc.wake_trigger.value,
                "verdict": state.last_verdict.value if state else "unknown",
                "verdict_reason": (state.notes or "") if state else "",
                "down_label": _down_label(state),
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
            # For the bulk "Set policy" action.
            "backup_policies": [
                {"id": p.id, "name": p.name}
                for p in session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
            ],
        },
    )


@router.get("/services/{slug}", response_model=None)
async def service_detail_page(
    slug: str, request: Request, session: SessionDep, settings: SettingsDep
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
    tree = TreeIndex.build(session)
    below = {d.slug for d in tree.descendants(svc)}
    parent_choices = [s for s in other_slugs if s not in below]
    probe_kinds = [
        {"kind": k.value, "summary": spec.summary, "default_timeout_s": spec.default_timeout_s}
        for k, spec in PROBE_SPECS.items()
    ]
    diff = compute_registry_diff(session)
    state = session.get(ServiceState, svc.id) if svc.id is not None else None
    last_results = state.last_probe_results if state else []
    health = {
        "verdict": state.last_verdict.value if state else "unknown",
        "verdict_at": (
            state.last_verdict_at.isoformat() if state and state.last_verdict_at else None
        ),
        "reason": (state.notes or "") if state else "",
        "down_label": _down_label(state),
        "guest": guest_gate_view(state),
        # Last scan's result per probe name, for the Probes tab ("skipped —
        # guest stopped" when the guest gate kept them from running).
        "probe_results": {
            str(r.get("probe_name")): _probe_row(r)
            for r in last_results
            if r.get("kind") != "guest"
        },
    }

    return _templates.TemplateResponse(
        request,
        "service_detail.html",
        {
            "admin": admin,
            "health": health,
            "service": service_detail(session, svc),
            "nodes": [{"id": n.id, "name": n.name} for n in nodes],
            "policies": [{"id": p.id, "name": p.name} for p in policies],
            "other_slugs": other_slugs,
            "parent_choices": parent_choices,
            "requirable": _requirable_services(session, exclude=slug),
            "existing_groups": existing_groups,
            "guest_kinds": [k.value for k in GuestKind],
            "probe_kinds": probe_kinds,
            "providers": [p.value for p in RouterProvider],
            "registry_dirty": diff.dirty,
            "yaml_path": diff.yaml_path,
            "ping_transport": _ping_transport(settings.ip_discovery_transport),
        },
    )


def _ping_transport(raw: str) -> dict[str, Any] | None:
    """The one-click ping probe's transport: IP_DISCOVERY_TRANSPORT (pinging
    from where ARP is read), or None (= local) when unset or local."""
    block = transport_block(raw)
    if not block or str(block.get("type", "local")) in ("local", ""):
        return None
    return block


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
    the existing ``POST /api/proxy-hosts`` (database only; a ``traefik`` host
    with an upstream is pushed on Save to YAML / Push now - see
    ``web/routers/proxy_hosts.py``).
    ``?service_id=`` pre-fills the service picker — used by the "Add proxy
    host" link on a service's own detail page.
    """
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/proxy-hosts/new", status_code=303)

    return _templates.TemplateResponse(
        request,
        "proxy_host_form.html",
        {
            "admin": admin,
            "mode": "new",
            "proxy_host": None,
            "initial_service_id": service_id,
            "services": _service_choices(session),
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
                "entry_points": list(ph.entry_points or []),
                "cert_resolver": ph.cert_resolver or "",
                "entry_points_stored": bool(ph.entry_points),
                "route_options": RouteOptions.from_dict(ph.route_options).to_dict(),
                "enabled": ph.enabled,
            },
            "initial_service_id": None,
            "services": _service_choices(session),
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
        "services": [
            {
                "slug": s.slug,
                "group": s.group,
                # never backed up (e.g. the VM hosting the PBS datastore): not selectable
                "excluded": s.backup_excluded,
                "reason": s.backup_excluded_reason,
            }
            for s in services
        ],
        "requirable": _requirable_services(session),
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


def _range_param(value: str | None) -> str:
    return value if value in RANGES else DEFAULT_RANGE


@router.get("/traffic", response_model=None)
async def traffic_page(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    range: str | None = None,
    slug: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Requests / status classes / latency over time, rolled up from
    Traefik's access log by ``orchestrator.scheduler.access_log_collector``.
    The page is a shell: ``static/traffic.js`` fetches
    ``/api/traffic/series`` and draws the charts; the collector status strip
    is server-rendered for the first paint. See ``docs/traffic_metrics.md``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/traffic", status_code=303)

    return _templates.TemplateResponse(
        request,
        "traffic.html",
        {
            "admin": admin,
            "collector": collector_status_payload(session, settings),
            "range": _range_param(range),
            "slug": (slug or "").strip(),
        },
    )


def _flag_emoji(country_code: str) -> str:
    """ISO 3166-1 alpha-2 -> flag emoji via regional-indicator symbols
    ("NL" -> U+1F1F3 U+1F1F1). Blank for anything that is not exactly two
    ASCII letters (local/unknown buckets carry an empty code)."""
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def _geo_label(bucket: str, country_name: str, country_code: str) -> str:
    if bucket == "local":
        return "Local network"
    return country_name or country_code or "Unknown"


def _decorate_geo_view(view: dict[str, Any]) -> dict[str, Any]:
    """Add the page-only presentation bits (flag, label, share-of-total
    percentage, local/external summary) on top of :func:`build_geo_view`'s
    API-shaped dict, leaving the original keys untouched."""
    total = sum(c["hits"] for c in view["countries"])
    local = sum(c["hits"] for c in view["countries"] if c["bucket"] == "local")
    countries = [
        {
            **c,
            "flag": "" if c["bucket"] == "local" else _flag_emoji(c["country_code"]),
            "label": _geo_label(c["bucket"], c["country_name"], c["country_code"]),
            "pct": round(c["hits"] / total * 100, 1) if total else 0.0,
        }
        for c in view["countries"]
    ]
    names = {c["country_code"]: c["country_name"] for c in view["countries"] if c["country_code"]}
    top_ips = [
        {
            **i,
            "flag": "" if i["bucket"] == "local" else _flag_emoji(i["country_code"]),
            "label": _geo_label(i["bucket"], names.get(i["country_code"], ""), i["country_code"]),
        }
        for i in view["top_ips"]
    ]
    return {
        **view,
        "countries": countries,
        "top_ips": top_ips,
        "total_hits": total,
        "local_hits": local,
        "external_hits": total - local,
        "local_pct": round(local / total * 100, 1) if total else 0.0,
        "external_pct": round((total - local) / total * 100, 1) if total else 0.0,
        "country_count": sum(1 for c in view["countries"] if c["bucket"] == "country"),
    }


@router.get("/traffic/geo", response_model=None)
async def traffic_geo_page(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    slug: str | None = None,
    range: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Traffic by country / client IP — the dashboard page over
    :func:`orchestrator.web.routers.geo_traffic.build_geo_view` (the same
    aggregation ``GET /api/traffic/geo`` returns). Server-rendered like
    ``/traffic``. See ``docs/geoip_traffic.md``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/traffic/geo", status_code=303)

    slug = (slug or "").strip() or None
    range_key = _range_param(range)
    view = _decorate_geo_view(
        build_geo_view(session, enabled=settings.geoip_enabled, slug=slug, range_key=range_key)
    )
    services = session.exec(select(Service).order_by(Service.slug)).all() if view["enabled"] else []

    return _templates.TemplateResponse(
        request,
        "traffic_geo.html",
        {
            "admin": admin,
            "view": view,
            "services": services,
            "slug": slug or "",
            "range": range_key,
            "collector": collector_status_payload(session, settings),
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
    node: str | None = Query(None),
    guest: str | None = Query(None),
    kind: str | None = Query(None),
    problems: bool = Query(False),
    since_hours: str | None = Query(None),
    q: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """History of wakes, backups, admin actions and alerts — see ``routers/activity.py``."""
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/activity", status_code=303)

    # the filter form submits "" for "all": normalise every field
    service, group, node, guest = (
        (service or None),
        (group or None),
        (node or None),
        (guest or None),
    )
    kind = kind if kind in KINDS else None
    hours = (
        int(since_hours) if since_hours and since_hours.isdigit() and int(since_hours) > 0 else None
    )
    q = (q or "").strip() or None
    events = build_feed(
        session,
        service=service,
        group=group,
        node=node,
        guest=guest,
        kind=kind,
        problems=problems,
        since_hours=hours,
        q=q,
    )
    return _templates.TemplateResponse(
        request,
        "activity.html",
        {
            "admin": admin,
            "events": events,
            "f": {
                "service": service,
                "group": group,
                "node": node,
                "guest": guest,
                "kind": kind,
                "problems": problems,
                "since_hours": hours,
                "q": q or "",
            },
            "options": filter_options(session),
            "filtered": any((service, group, node, guest, kind, problems, hours, q)),
        },
    )
