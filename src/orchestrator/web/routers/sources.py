"""Data sources: what the orchestrator fetches from other systems, and the actions that pull it.

Nodes and guests belong to Proxmox, routes to Traefik Manager, stacks to Portainer, addresses to
the guests themselves. None of that is created or edited here - it is *pulled*: listed as the
orchestrator last saw it, refreshed on demand, and (for routes) rewritten into one standard
shape. The same actions exist on the older pages' APIs; this is where they live now. See
docs/sources.md.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from orchestrator.audit import log as audit
from orchestrator.db.models import (
    Node,
    PipelineRun,
    ProxyHost,
    Service,
    ServiceState,
    Setting,
)
from orchestrator.db.session import session_scope
from orchestrator.domain.enums import PipelineKind, PipelineStatus
from orchestrator.ipdiscovery.discovery import ip_view
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.pipelines.steps import StepRecorder
from orchestrator.registry import route_format as rf
from orchestrator.registry.proxy_push import ref_of
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

log = get_logger(__name__)
router = APIRouter(prefix="/api/sources", tags=["sources"], dependencies=[Depends(require_admin)])

_SETTING = "sources.refreshed"
_background: set[asyncio.Task[None]] = set()


def _stamp(session: Session, source: str) -> None:
    row = session.get(Setting, _SETTING) or Setting(key=_SETTING, value={})
    row.value = {**row.value, source: datetime.now(UTC).isoformat()}
    row.updated_at = datetime.now(UTC)
    session.add(row)


def _stamps(session: Session) -> dict[str, str]:
    row = session.get(Setting, _SETTING)
    return dict(row.value) if row else {}


# -- overview ---------------------------------------------------------------------------------


@router.get("/overview")
async def overview(session: SessionDep, adapters: AdaptersDep) -> dict[str, Any]:
    """One line per source: what is known and when it was last pulled."""
    services = session.exec(select(Service)).all()
    states = {s.service_id: s for s in session.exec(select(ServiceState)).all()}
    with_guest = [s for s in services if s.guest_id is not None]
    no_ip = [
        s
        for s in with_guest
        if not (s.ip_address or (states.get(s.id) and states[s.id].discovered_ip))
    ]
    hosts = session.exec(select(ProxyHost)).all()
    portainers = getattr(adapters, "portainers", {})
    return {
        "refreshed": _stamps(session),
        "proxmox": {"nodes": len(session.exec(select(Node)).all()), "guests": len(with_guest)},
        "ips": {"services": len(with_guest), "missing": len(no_ip)},
        "traefik": {
            "hosts": len(hosts),
            "never_pushed": sum(1 for h in hosts if h.enabled and not (h.pushed_sig or "")),
        },
        "portainer": {
            "configured": any(a.configured for a in portainers.values()),
            "instances": len(portainers),
        },
    }


# -- Proxmox: nodes and guests -----------------------------------------------------------------


@router.get("/proxmox")
async def proxmox(session: SessionDep, adapters: AdaptersDep) -> dict[str, Any]:
    from orchestrator.web.routers import infra

    nodes = await infra.nodes_status(session, adapters)
    try:
        guests = await infra.list_guests(adapters, session, None)
        error = None
    except HTTPException as exc:
        guests, error = [], str(exc.detail)
    stored = {n.name: n for n in session.exec(select(Node)).all()}
    for row in nodes.get("nodes", []):
        node = stored.get(row["name"])
        row["power_target"] = node.power_mgr_target if node else None
        row["registered"] = node is not None
    # Nodes the cluster lists that have no record here yet (importable).
    try:
        cluster = await adapters.proxmox.cluster_status()
        known = {r["name"] for r in nodes.get("nodes", [])}
        for entry in cluster.nodes:
            if entry.name not in known:
                nodes.setdefault("nodes", []).append(
                    {
                        "name": entry.name,
                        "registered": False,
                        "address": entry.ip,
                        "proxmox_online": entry.online,
                        "state": "on" if entry.online else "off",
                        "power_target": None,
                        "always_on": False,
                    }
                )
    except Exception:  # the node list above already carries the error
        pass
    return {
        "nodes": nodes.get("nodes", []),
        "proxmox_error": nodes.get("proxmox_error"),
        "guests": guests,
        "guests_error": error,
        "refreshed": _stamps(session).get("proxmox"),
    }


@router.post("/proxmox/refresh")
async def proxmox_refresh(
    session: SessionDep, adapters: AdaptersDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Pull from Proxmox: fill blank node addresses, check every node, re-read every guest and
    scan the services on them. Addresses set by hand are never overwritten."""
    from orchestrator.web.routers import infra

    filled = await infra.fill_node_addresses(session, adapters, settings, admin)
    checks = []
    for node in session.exec(select(Node).order_by(Node.name)).all():
        checks.append({"node": node.name, **await infra.node_health_view(adapters, node)})
    resync = await infra.do_resync(session, adapters, settings, admin.username)
    _stamp(session, "proxmox")
    session.commit()
    return {"addresses": filled, "nodes": checks, "guests": resync}


class NodeImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


@router.post("/proxmox/nodes/import", status_code=status.HTTP_201_CREATED)
async def import_node(
    body: NodeImportIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    """Record a node the cluster has. Its name and address come from Proxmox; what only this
    orchestrator knows (power target, always on) is set on the dashboard afterwards."""
    from orchestrator.web.routers import dashboard

    try:
        cluster = await adapters.proxmox.cluster_status()
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    entry = next((n for n in cluster.nodes if n.name == body.name), None)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Proxmox has no node named {body.name!r}")
    address = settings.proxmox_host if entry.local and settings.proxmox_host else entry.ip
    out = await dashboard.create_node(
        dashboard.NodeCreate(name=entry.name, address=address or ""), session, admin
    )
    _stamp(session, "proxmox")
    session.commit()
    return out


class GuestImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    vmid: int
    slug: str | None = None
    name: str | None = None
    group: str = ""


@router.post("/proxmox/guests/import", status_code=status.HTTP_201_CREATED)
async def import_guest(
    body: GuestImportIn, session: SessionDep, adapters: AdaptersDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Register a Proxmox guest nobody points at yet as a (root) service: name, kind, id and
    node come from Proxmox. Health probes and the rest are added on its own page."""
    import re

    from orchestrator.web.routers import dashboard

    try:
        guests = await adapters.proxmox.list_guests(body.node)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    guest = next((g for g in guests if g.vmid == body.vmid and g.node == body.node), None)
    if guest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no guest {body.vmid} on {body.node!r}")
    if session.exec(select(Service).where(Service.guest_id == body.vmid)).first() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"guest {body.vmid} already belongs to a service"
        )
    node = session.exec(select(Node).where(Node.name == body.node)).one_or_none()
    if node is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"import node {body.node!r} first - a service lives on a node"
        )
    slug = (
        body.slug or re.sub(r"[^a-z0-9]+", "-", guest.name.lower()).strip("-") or f"vm-{guest.vmid}"
    )
    out = await dashboard.create_service(
        dashboard.ServiceCreate(
            slug=slug,
            name=body.name or guest.name or slug,
            group=body.group,
            node_id=node.id,
            guest_kind=guest.kind.value,
            guest_id=guest.vmid,
        ),
        session,
        admin,
    )
    _stamp(session, "proxmox")
    session.commit()
    return out


# -- addresses -----------------------------------------------------------------------------------


@router.get("/ips")
async def ips(session: SessionDep) -> list[dict[str, Any]]:
    nodes = {n.id: n.name for n in session.exec(select(Node)).all()}
    states = {s.service_id: s for s in session.exec(select(ServiceState)).all()}
    out = []
    for svc in session.exec(select(Service).order_by(Service.slug)).all():
        if svc.guest_id is None:
            continue
        view = ip_view(svc, states.get(svc.id))
        out.append(
            {
                "slug": svc.slug,
                "name": svc.name,
                "node": nodes.get(svc.node_id),
                "guest": f"{svc.guest_kind.value}/{svc.guest_id}",
                **{
                    k: view[k]
                    for k in (
                        "effective_ip",
                        "effective_source_label",
                        "manual_ip",
                        "discovered_ip",
                        "mac_addresses",
                        "ip_discovered_at",
                        "ip_discovery_error",
                    )
                },
            }
        )
    return out


class DiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str | None = None
    #: Only services that have no address yet.
    only_missing: bool = False


@router.post("/ips/discover")
async def ips_discover(
    body: DiscoverIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    """Run address discovery (guest agent / LXC, ARP, DHCP) for every service with a guest, or
    one node's, or only those without an address. Never touches an address set by hand."""
    from orchestrator.web.routers.dashboard import discover_service_ip

    stmt = select(Service).where(Service.guest_id.is_not(None)).order_by(Service.slug)  # type: ignore[union-attr]
    if body.node:
        node = session.exec(select(Node).where(Node.name == body.node)).one_or_none()
        if node is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no node named {body.node!r}")
        stmt = stmt.where(Service.node_id == node.id)
    states = {s.service_id: s for s in session.exec(select(ServiceState)).all()}
    results: list[dict[str, Any]] = []
    for svc in session.exec(stmt).all():
        st = states.get(svc.id)
        if body.only_missing and (svc.ip_address or (st and st.discovered_ip)):
            continue
        try:
            got = await discover_service_ip(
                session, adapters, settings, svc.slug, actor=admin.username
            )
            results.append(
                {
                    "slug": svc.slug,
                    "ok": True,
                    "ip": got.get("effective_ip") or "",
                    "detail": got.get("ip_discovery_error") or "",
                }
            )
        except HTTPException as exc:
            results.append({"slug": svc.slug, "ok": False, "ip": "", "detail": str(exc.detail)})
        except Exception as exc:
            results.append(
                {"slug": svc.slug, "ok": False, "ip": "", "detail": f"{type(exc).__name__}: {exc}"}
            )
    _stamp(session, "ips")
    session.commit()
    return {
        "scanned": len(results),
        "found": sum(1 for r in results if r["ip"]),
        "results": results,
    }


# -- Traefik Manager -------------------------------------------------------------------------------


@router.get("/traefik")
async def traefik(session: SessionDep, adapters: AdaptersDep) -> dict[str, Any]:
    """Every route Traefik Manager has next to the host record it belongs to."""
    proxy = adapters.proxy
    routes = await proxy.list_routes()
    hosts = session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all()
    services = {s.id: s for s in session.exec(select(Service)).all()}
    by_host = {h.hostname.lower(): h for h in hosts}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in routes or []:
        names = sorted(r.hostnames)
        ph = next((by_host[h.lower()] for h in names if h.lower() in by_host), None)
        if ph is not None:
            seen.add(ph.hostname.lower())
        rows.append(
            {
                "route": r.name,
                "hostnames": names,
                "upstream": r.upstream,
                "file": r.config_file,
                "enabled": r.enabled,
                "entry_points": list(r.entry_points),
                "cert_resolver": r.cert_resolver,
                "owned": proxy.owns_route_ref(r.name),
                "redirect_companion": r.name.endswith("-redirect"),
                "host_id": ph.id if ph else None,
                "service": (
                    services[ph.service_id].slug if ph and ph.service_id in services else None
                ),
                "host_enabled": ph.enabled if ph else None,
            }
        )
    return {
        "readable": routes is not None,
        "routes": rows,
        "no_route": [
            {
                "host_id": h.id,
                "hostname": h.hostname,
                "enabled": h.enabled,
                "ref": ref_of(h),
                "never_pushed": not (h.pushed_sig or ""),
                "service": services[h.service_id].slug if h.service_id in services else None,
            }
            for h in hosts
            if h.hostname.lower() not in seen and h.router_provider.value == "traefik"
        ],
        "refreshed": _stamps(session).get("traefik"),
    }


@router.post("/traefik/import")
async def traefik_import(
    session: SessionDep, adapters: AdaptersDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Pull Traefik Manager's routes into the records (what "Resync from Traefik" did)."""
    from orchestrator.web.routers import proxy_hosts

    out = await proxy_hosts.resync_proxy_hosts(session, admin, settings, adapters)
    _stamp(session, "traefik")
    session.commit()
    return out


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostnames: list[str] | None = None
    #: True = put the HSTS middleware on every formatted route, False = take it off,
    #: None (default) = keep what each route has.
    hsts: bool | None = None


@router.post("/traefik/format/plan")
async def format_plan(
    body: PlanIn, session: SessionDep, adapters: AdaptersDep, settings: SettingsDep
) -> dict[str, Any]:
    """What formatting would do to each host. Changes nothing."""
    plan = await rf.build_plan(
        session, adapters, settings, hostnames=body.hostnames, hsts=body.hsts
    )
    return plan.as_dict()


class FormatItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    #: From the plan the person looked at: a route that changed since is skipped.
    fingerprint: str
    #: Middlewares to keep (default: the plan's). Only ones the route already has count.
    middlewares: list[str] | None = None


class FormatRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[FormatItemIn] = Field(min_length=1)
    stop_on_failure: bool = True
    #: Accept a route Traefik's runtime list could not confirm (no Traefik API reachable).
    allow_unverified: bool = False
    #: As in the plan the person looked at (the run plans again with the same choice).
    hsts: bool | None = None


@router.post("/traefik/format/run", status_code=status.HTTP_202_ACCEPTED)
async def format_run(
    body: FormatRunIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    """Rewrite the chosen hosts, one at a time, in the background. Poll ``/api/runs/{id}``."""
    running = session.exec(
        select(PipelineRun).where(
            PipelineRun.kind == PipelineKind.FORMAT, PipelineRun.status == PipelineStatus.RUNNING
        )
    ).first()
    if running is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a format run is already going (run {running.id})"
        )
    run = PipelineRun(
        kind=PipelineKind.FORMAT,
        correlation_id=new_correlation_id(),
        status=PipelineStatus.RUNNING,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    audit.record(
        session,
        actor=admin.username,
        action="proxy.format_start",
        target="proxy_hosts",
        correlation_id=run.correlation_id,
        details={"run_id": run.id, "hosts": [i.hostname for i in body.items]},
    )
    session.commit()
    run_id, actor = run.id, admin.username
    assert run_id is not None

    async def go() -> None:
        try:
            with session_scope() as s:
                await _execute(s, adapters, settings, run_id, body, actor)
        except Exception:
            log.exception("route_format.run_crashed", run_id=run_id)

    task = asyncio.create_task(go(), name=f"format:{run_id}")
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"run_id": run_id, "status": "running", "hosts": len(body.items)}


async def _execute(
    session: Session, adapters: Any, settings: Any, run_id: int, body: FormatRunIn, actor: str
) -> None:
    run = session.get(PipelineRun, run_id)
    assert run is not None
    rec = StepRecorder(session, run)
    error: str | None = None
    ok = True
    try:
        wanted = {i.hostname.lower(): i for i in body.items}
        plan = await rf.build_plan(
            session, adapters, settings, hostnames=list(wanted), hsts=body.hsts
        )
        if plan.problem:
            raise rf.FormatFailed(plan.problem)
        keep: list[rf.FormatItem] = []
        for item in plan.items:
            chosen = wanted[item.hostname.lower()]
            if item.status != rf.READY:
                rec.record(item.hostname, "skipped", detail={"reason": "; ".join(item.reasons)})
                continue
            if item.fingerprint != chosen.fingerprint:
                rec.record(
                    item.hostname, "skipped", detail={"reason": "the route changed since the plan"}
                )
                ok = False
                continue
            if chosen.middlewares is not None:
                have = list(item.current["middlewares"])
                item.target["middlewares"] = [m for m in chosen.middlewares if m in have]
                item.dropped_middlewares = [m for m in have if m not in item.target["middlewares"]]
            keep.append(item)
        plan.items = keep
        results = await rf.apply_plan(
            session,
            adapters,
            settings,
            plan,
            actor=actor,
            rec=rec,
            stop_on_failure=body.stop_on_failure,
            allow_unverified=body.allow_unverified,
        )
        ok = ok and all(r.ok for r in results) and len(results) == len(keep)
        if not ok:
            error = (
                "; ".join(f"{r.hostname}: {r.message}" for r in results if not r.ok)
                or "some hosts were not formatted"
            )
        _stamp(session, "traefik")
    except Exception as exc:
        ok, error = False, f"{type(exc).__name__}: {exc}"
        rec.record("crashed", "failed", detail={"error": error})
    run = session.get(PipelineRun, run_id) or run
    run.status = PipelineStatus.SUCCEEDED if ok else PipelineStatus.FAILED
    run.error = error
    run.finished_at = datetime.now(UTC)
    session.add(run)
    audit.record(
        session,
        actor=actor,
        action="proxy.format_done",
        target="proxy_hosts",
        correlation_id=run.correlation_id,
        details={"run_id": run_id, "ok": ok, "error": error},
    )
    session.commit()


# -- Portainer ------------------------------------------------------------------------------------


@router.get("/portainer")
async def portainer(
    session: SessionDep, adapters: AdaptersDep, instance: str | None = None
) -> dict[str, Any]:
    """Stacks and which services they are linked to (adopt them from here). ``instance`` is
    the Portainer connection; blank = the default one."""
    from orchestrator.web.routers import portainer as pr

    adapter = adapters.portainer_for(instance)
    if adapter is None or not adapter.configured:
        return {"configured": False, "stacks": [], "errors": {}, "refreshed": None}
    out = await pr.stacks(adapters, session, None, instance)
    _stamp(session, "portainer")
    session.commit()
    return {"configured": True, **out, "refreshed": _stamps(session).get("portainer")}
