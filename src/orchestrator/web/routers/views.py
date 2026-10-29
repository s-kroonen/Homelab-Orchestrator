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
    Probe,
    ProxyHost,
    Service,
    ServiceState,
)
from orchestrator.domain.enums import BackupMode, GuestKind, PipelineKind, PipelineStatus
from orchestrator.pipelines.wake import traffic_blocker
from orchestrator.registry.probe_specs import PROBE_SPECS
from orchestrator.registry.topology import build_map
from orchestrator.registry.tree import TreeIndex
from orchestrator.web.auth import current_device, get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.routers.auth import credentials_view, invites_view
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
        elif run is not None and (backup_failed or run.status is PipelineStatus.SKIPPED):
            backup = {
                "main": policies.get(svc.backup_policy_id, "?"),
                "sub": "failed" if backup_failed else "skipped",
                "why": run.error or "",
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
        wake_why = ""
        blocked = traffic_blocker(session, svc, node=node, served=tree.is_served(svc))
        if parent is not None and not svc.wake_on_hit_enabled:
            wake = "via parent"
        elif blocked is not None:
            wake = "no"
            wake_why = blocked
        elif node is not None and not node.always_on and not node.wake_on_hit_enabled:
            # On now, so a hit starts the guest; once the node is off, traffic will not wake it.
            wake = "while on"
            wake_why = f"its node {node.name} has wake on hit off: traffic does not turn it on"
        else:
            wake = "yes (button)" if svc.wake_trigger.value == "button" else "yes"
        rows.append(
            {
                "id": svc.id,
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
                "kind": _sub_kind(svc),
                "stack": _stack_ref(svc),
            }
        )
    _roll_up(session, rows)
    return rows


def _sub_kind(svc: Service) -> str:
    """What a sub-service is, in a word: a Portainer ``stack`` or ``container``, the
    controller's type, or ""."""
    cfg = svc.controller or {}
    if cfg.get("type") == "portainer":
        return "stack" if cfg.get("stack") else "container"
    return str(cfg.get("type") or "")


def _stack_ref(svc: Service) -> dict[str, Any] | None:
    """Where the stack page of a stack sub-service is: {instance, endpoint, name, href}."""
    from orchestrator.registry.stack_fold import stack_link

    link = stack_link(svc)
    if link is None:
        return None
    return stack_ref(str(link.get("instance") or ""), int(link["endpoint"]), str(link["stack"]))


def stack_ref(instance: str, endpoint: int, name: str) -> dict[str, Any]:
    from urllib.parse import quote

    path = f"/portainer/{endpoint}/{quote(name, safe='')}"
    return {
        "instance": instance,
        "endpoint": endpoint,
        "name": name,
        "href": path + (f"?instance={quote(instance, safe='')}" if instance else ""),
    }


#: A sub-service in one of these states makes its (healthy) parent "degraded".
_FAILING = {"failed", "stopped"}


def _roll_up(session: Session, rows: list[dict[str, Any]]) -> None:
    """A parent's state takes in its sub-services: healthy itself with a failed or stopped
    sub-service below it is "degraded". Each row also gets ``subs`` (its direct children's
    slugs) and ``sub_failing`` (the failing ones anywhere below it), and a stack its number of
    containers (its ``container:`` checks)."""
    from orchestrator.registry.stack_fold import PREFIX

    by_slug = {r["slug"]: r for r in rows}
    containers: dict[int, int] = {}
    for p in session.exec(select(Probe)).all():
        if p.name.startswith(PREFIX):
            containers[p.service_id] = containers.get(p.service_id, 0) + 1
    for r in rows:
        r["subs"] = []
        r["sub_failing"] = []
        r["containers"] = containers.get(r["id"], 0)
    for r in rows:
        if r["parent"] in by_slug:
            by_slug[r["parent"]]["subs"].append(r["slug"])
    for r in rows:
        if not r["enabled"] or r["state"]["label"] not in _FAILING:
            continue
        up = by_slug.get(r["parent"] or "")
        while up is not None:
            up["sub_failing"].append(r["name"])
            up = by_slug.get(up["parent"] or "")
    for r in rows:
        if r["sub_failing"] and r["state"]["label"] in ("healthy", "backup failed"):
            r["own_state"] = r["state"]
            names = r["sub_failing"]
            r["state"] = {
                "cls": "warn",
                "label": "degraded",
                "why": f"healthy itself; {', '.join(names[:3])}"
                + (f" and {len(names) - 3} more" if len(names) > 3 else "")
                + (" is" if len(names) == 1 else " are")
                + " failing",
            }


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


def _node_boot(session: Session, node: Node) -> dict[str, Any]:
    """What Proxmox does when the node boots, from the kept copy of its settings."""
    from orchestrator.db.models import NodeState
    from orchestrator.registry import boot as boot_mod

    state = session.get(NodeState, node.id) if node.id is not None else None
    took, learned = boot_mod.node_boot_s(session, node)
    return {
        "delay_s": state.onboot_delay_s if state else None,
        "read_at": state.boot_read_at.isoformat() if state and state.boot_read_at else None,
        "sequence": [x.as_dict() for x in boot_mod.sequence(session, node)],
        "boot_s": took,
        "boot_learned": learned,
    }


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
                "boot": _node_boot(session, n),
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
async def admins_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    """Your passkeys and the invite links (the Admins wireframe)."""
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    initial = {
        "credentials": credentials_view(session, admin, current_device(request)),
        "invites": invites_view(session, settings),
    }
    return templates.TemplateResponse(request, "admins.html", {"admin": admin, "initial": initial})


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


def _subs_view(
    session: Session, rows: list[dict[str, Any]], row: dict[str, Any]
) -> list[dict[str, Any]]:
    """The Sub-services tab: everything below a service, in tree order, with what each is,
    its checks, whether a backup waits for it and its proxy hosts."""
    by_slug = {r["slug"]: r for r in rows}
    out = []
    for slug in _below(by_slug, row):
        r = by_slug[slug]
        svc = session.get(Service, r["id"])
        if svc is None:
            continue
        probes = [
            p for p in session.exec(select(Probe).where(Probe.service_id == svc.id)) if p.enabled
        ]
        waits = sorted(p.name for p in probes if p.backup_waits)
        state = session.get(ServiceState, svc.id)
        results = {
            str(x.get("probe_name")): x.get("state")
            for x in (state.last_probe_results if state else [])
            if x.get("kind") != "guest" and not (x.get("details") or {}).get("skipped")
        }
        ok = sum(1 for p in probes if results.get(p.name) == "healthy")
        hosts = session.exec(select(ProxyHost).where(ProxyHost.service_id == svc.id)).all()
        out.append(
            {
                **_sub_brief(r),
                "parent": r["parent"],
                "why": r["state"]["why"],
                "check": r["check"],
                "checks": {"ok": ok, "total": len(probes), "run": bool(results)},
                "backup": (
                    "waits for it"
                    if svc.backup_gate.value == "required"
                    else (
                        f"waits for {', '.join(w.removeprefix('container:') for w in waits)}"
                        if waits
                        else "does not wait"
                    )
                ),
                "hosts": [h.hostname for h in hosts],
                "controller": dict(svc.controller or {}),
                "enabled": svc.enabled,
            }
        )
    return out


def service_view(session: Session, settings: Any, svc: Service) -> dict[str, Any]:
    """Everything the service page shows, from the database only."""
    detail = service_detail(session, svc)
    rows = service_rows(session)
    row = next((r for r in rows if r["slug"] == svc.slug), None)
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
        "subs": _subs_view(session, rows, row) if row else [],
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
    by_slug = {r["slug"]: r for r in rows}
    for r in rows:
        if r["parent"]:
            continue  # a sub-service shows up under its parent ("degraded")
        st = r["state"]
        if st["label"] == "degraded":
            first = next(
                (
                    by_slug[s]
                    for s in _below(by_slug, r)
                    if by_slug[s]["state"]["label"] in _FAILING and by_slug[s]["enabled"]
                ),
                None,
            )
            items.append(
                {
                    "kind": "degraded",
                    "cls": "warn",
                    "label": "Degraded",
                    "slug": r["slug"],
                    "title": f"{r['name']}: sub-service {first['name'] if first else '?'} "
                    + (first["state"]["label"] if first else "failing"),
                    "text": (
                        (first["check"]["text"] or first["state"]["why"]) if first else st["why"]
                    ),
                    "at": first["check"]["at"] if first else None,
                    "action": "check",
                    "sub": first["slug"] if first else None,
                    "stack": first.get("stack") if first else None,
                }
            )
            st = r.get("own_state", st)
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
        else:
            run = runs.get(ids.get(r["slug"]) or -1)
            if run is not None and run.status is PipelineStatus.SKIPPED:
                items.append(
                    {
                        "kind": "backup",
                        "cls": "warn",
                        "label": "Backup",
                        "slug": r["slug"],
                        "title": f"{r['name']} backup skipped",
                        "text": run.error or "its last backup was skipped",
                        "at": (run.finished_at or run.started_at).isoformat(),
                        "action": "run",
                    }
                )
    return items


def _sub_brief(r: dict[str, Any]) -> dict[str, Any]:
    """A sub-service as the dropdowns show it."""
    return {
        "slug": r["slug"],
        "name": r["name"],
        "cls": r["state"]["cls"],
        "label": r["state"]["label"],
        "kind": r["kind"],
        "containers": r["containers"],
        "stack": r["stack"],
        "depth": r["depth"],
    }


def _below(by_slug: dict[str, dict[str, Any]], row: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for s in row["subs"]:
        out.append(s)
        out += _below(by_slug, by_slug[s])
    return out


def dashboard_view(session: Session, scheduler: Any) -> dict[str, Any]:
    rows = service_rows(session)
    by_slug = {r["slug"]: r for r in rows}
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
                "subs": [_sub_brief(by_slug[x]) for x in _below(by_slug, r)],
            }
        )
    policies = [
        policy_to_dict(session, p, scheduler)
        for p in session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
    ]
    nxt = sorted((p for p in policies if p["next_run"]), key=lambda p: p["next_run"])
    last = _last_runs(session)
    counts: dict[str, int] = {}
    tops = [r for r in rows if not r["parent"]]  # a sub-service counts in its parent's state
    for r in tops:
        counts[r["state"]["cls"]] = counts.get(r["state"]["cls"], 0) + 1
    return {
        "attention": _attention(session, rows),
        "services": {"total": len(tops), "by_state": counts},
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


# --- map --------------------------------------------------------------------------------------


def map_view(session: Session, settings: Any, scheduler: Any) -> dict[str, Any]:
    """The Map wireframe: :func:`build_map` (nodes, guests, hosts, the backup server and the
    lines between them) plus what the selection sidebar and its actions need: every service's
    row as on the Services page, the nodes as on Nodes & guests, the policies with their next run.
    """
    view = services_view(session)
    return {
        "map": build_map(session, settings),
        "view": view,
        "nodes": nodes_view(session)["nodes"],
        "policies": [
            {
                "name": p["name"],
                "next_run": p["next_run"],
                "restore_power_state": p["restore_power_state"],
            }
            for p in (
                policy_to_dict(session, pol, scheduler)
                for pol in session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
            )
        ],
    }


@api.get("/map")
async def map_api(request: Request, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    return map_view(session, settings, getattr(request.app.state, "scheduler", None))


@pages.get("/map", response_model=None)
async def map_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    """Every node in a row with the hosts that lead to it and the backup server they share;
    click anything to trace it in the selection sidebar (the Map wireframe)."""
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "admin": admin,
            "initial": map_view(session, settings, getattr(request.app.state, "scheduler", None)),
        },
    )
