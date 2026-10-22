"""Infrastructure inspection — connectivity checks, guests, snapshots — plus
one manual mutating action: node power.

Deliberately separate from ``/healthz``: those report on *us*, these report on
what we can see of Proxmox and PBS. An unreachable Proxmox is not an
orchestrator health problem.

``POST /nodes/{name}/power`` is the one write in this module: a manual,
admin-triggered power action straight through ``adapters.power`` (wake/
power_off/restart), not the wake pipeline. See :func:`node_power_action`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable
from orchestrator.audit import log as audit
from orchestrator.db.models import Node, Service, ServiceState
from orchestrator.domain.enums import GuestKind, PowerState
from orchestrator.registry.tree import TreeIndex
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api/infra", tags=["infra"], dependencies=[Depends(require_admin)])


async def _probe(label: str, coro: Any) -> dict[str, Any]:
    """Run a connectivity check, mapping the adapter error hierarchy onto a
    machine-readable reachability verdict."""
    try:
        result = await coro
    except AdapterUnreachable as exc:
        return {"target": label, "reachable": False, "error": str(exc), "kind": "unreachable"}
    except AdapterError as exc:
        # We got a response — it just wasn't a good one. Reachable but refusing.
        return {
            "target": label,
            "reachable": True,
            "ok": False,
            "error": str(exc),
            "kind": type(exc).__name__,
        }
    return {"target": label, "reachable": True, "ok": True, "result": result}


async def power_checks(session: Session, adapters: Any) -> list[dict[str, Any]]:
    """The power manager's own connectivity check plus one row per node's
    ``power_mgr_target``, in the shape :func:`_probe` produces."""
    nodes = session.exec(select(Node).order_by(Node.name)).all()
    targets = sorted({n.power_mgr_target.strip() for n in nodes if n.power_mgr_target.strip()})
    try:
        result = await asyncio.wait_for(adapters.power.check(targets), _POWER_CHECK_TIMEOUT_S)
    except TimeoutError:
        return [
            {
                "target": "power_manager",
                "reachable": False,
                "error": "the power manager did not answer",
                "kind": "unreachable",
            }
        ]
    except AdapterError as exc:
        return [
            {
                "target": "power_manager",
                "reachable": False,
                "error": str(exc),
                "kind": "unreachable",
            }
        ]

    if not result.connected:
        return [
            {
                "target": "power_manager",
                "reachable": False,
                "error": result.detail,
                "kind": "unreachable",
            }
        ]
    rows: list[dict[str, Any]] = [
        {
            "target": "power_manager",
            "reachable": True,
            "ok": True,
            "result": {"detail": result.detail, "endpoint": result.endpoint},
        }
    ]
    by_node = {n.power_mgr_target.strip(): n.name for n in nodes}
    for t in result.targets:
        label = f"power:{t.target} ({by_node.get(t.target, '?')})"
        if t.online:
            state = t.state.value if t.state else "unknown"
            rows.append(
                {
                    "target": label,
                    "reachable": True,
                    "ok": True,
                    "result": {"detail": f"online, power state {state}"},
                }
            )
        else:
            why = (
                "reports offline in the power manager"
                if t.online is False
                else (t.detail or "the power manager never reported this target")
            )
            rows.append({"target": label, "reachable": False, "error": why, "kind": "unreachable"})
    return rows


#: Bounds the whole power check (the MQTT adapter waits on its own too).
_POWER_CHECK_TIMEOUT_S = 15.0


async def node_checks(session: Session, adapters: Any) -> list[dict[str, Any]]:
    """One connectivity row per node that has an address: its own Proxmox API."""
    nodes = [n for n in session.exec(select(Node).order_by(Node.name)).all() if n.address.strip()]
    views = await asyncio.gather(*(node_health_view(adapters, n) for n in nodes))
    rows: list[dict[str, Any]] = []
    for node, v in zip(nodes, views, strict=True):
        label = f"node:{node.name} ({node.address})"
        if v.get("reachable"):
            bits = [f"Proxmox {v.get('version') or '?'}", f"{v.get('latency_ms')} ms"]
            if v.get("uptime_s") is not None:
                bits.append(f"up {int(v['uptime_s']) // 3600}h")
            rows.append(
                {
                    "target": label,
                    "reachable": True,
                    "ok": True,
                    "result": {"detail": ", ".join(bits)},
                }
            )
        else:
            rows.append(
                {
                    "target": label,
                    "reachable": False,
                    "error": v.get("error") or "no answer",
                    "kind": "unreachable",
                }
            )
    return rows


@router.get("/status")
async def infra_status(
    adapters: AdaptersDep, settings: SettingsDep, session: SessionDep
) -> dict[str, Any]:
    """One call that answers 'can I reach everything?' — what you run first."""
    pve = await _probe("proxmox", adapters.proxmox.version())
    if pve.get("ok"):
        pve["result"] = {"version": pve["result"].version, "release": pve["result"].release}

    pbs = await _probe("pbs", adapters.pbs.version())
    if pbs.get("ok"):
        pbs["result"] = {"version": pbs["result"].version, "release": pbs["result"].release}

    datastore = await _probe("pbs_datastore", adapters.pbs.datastore_status(settings.pbs_datastore))
    if datastore.get("ok"):
        ds = datastore["result"]
        datastore["result"] = {
            "name": ds.name,
            "total_bytes": ds.total_bytes,
            "used_bytes": ds.used_bytes,
            "available_bytes": ds.available_bytes,
            "used_fraction": round(ds.used_fraction, 4),
        }

    return {
        "dry_run": settings.dry_run,
        "pve_backup_storage": settings.pve_backup_storage,
        "pbs_datastore": settings.pbs_datastore,
        "checks": [
            pve,
            pbs,
            datastore,
            *await node_checks(session, adapters),
            *await power_checks(session, adapters),
        ],
    }


class NodePowerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["wake", "power_off", "restart"]
    #: Only meaningful for power_off/restart, matching ``PowerAdapter``'s own
    #: semantics exactly (see ``orchestrator/adapters/power/base.py``):
    #: False (default) asks for a graceful action, True skips straight to an
    #: immediate power cut / full power-cycle. Ignored for "wake".
    force: bool = False


#: A power manager that never answers must not stall the dashboard.
_POWER_STATUS_TIMEOUT_S = 4.0


def combined_node_state(
    power: PowerState | None, proxmox_online: bool | None, reachable: bool | None = None
) -> str:
    """One word for the dashboard: ``on`` / ``off`` / ``booting`` /
    ``stopping`` / ``unknown``. Proxmox seeing the node online wins (it is
    serving the API right now); otherwise the power manager's own view; a
    node Proxmox reports offline with no power-manager answer counts as off.
    ``reachable`` is the node's own API answering, which also proves it is on."""
    if proxmox_online or reachable:
        return "on"
    if power in (PowerState.ON, PowerState.OFF, PowerState.BOOTING, PowerState.STOPPING):
        return power.value
    if proxmox_online is False:
        return "off"
    return "unknown"


async def node_health_view(adapters: Any, node: Node) -> dict[str, Any]:
    """The node's own Proxmox API, asked directly at ``node.address``. Always a
    dict: ``checked`` False when there is no address to ask."""
    if not node.address.strip():
        return {"checked": False, "reachable": None, "error": "no address set"}
    try:
        h = await asyncio.wait_for(
            adapters.proxmox.node_health(node.name, node.address), _NODE_HEALTH_TIMEOUT_S
        )
    except TimeoutError:
        return {"checked": True, "reachable": False, "error": "no answer in time"}
    except Exception as exc:
        return {"checked": True, "reachable": False, "error": str(exc)}
    return {
        "checked": True,
        "reachable": h.reachable,
        "latency_ms": h.latency_ms,
        "version": h.version,
        "uptime_s": h.uptime_s,
        "cpu": h.cpu,
        "memory_used": h.memory_used,
        "memory_total": h.memory_total,
        "error": h.error,
    }


#: Per node; the dashboard asks for all of them at once.
_NODE_HEALTH_TIMEOUT_S = 10.0


@router.post("/nodes/{name}/check")
async def check_node(name: str, session: SessionDep, adapters: AdaptersDep) -> dict[str, Any]:
    """Health-check one node through its own Proxmox API."""
    node = session.exec(select(Node).where(Node.name == name)).one_or_none()
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no node named {name!r}")
    return {"node": node.name, "address": node.address, **await node_health_view(adapters, node)}


@router.post("/nodes/fill-addresses")
async def fill_node_addresses(
    session: SessionDep, adapters: AdaptersDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Fill blank node addresses from Proxmox: the node the setup talks to
    (``PROXMOX_HOST``) gets that host, the others the IP the cluster lists for
    them. Addresses already set (by hand) are never overwritten."""
    try:
        cluster = await adapters.proxmox.cluster_status()
    except AdapterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    by_name = {n.name: n for n in cluster.nodes}
    filled: dict[str, str] = {}
    skipped: list[str] = []
    for node in session.exec(select(Node).order_by(Node.name)).all():
        if node.address.strip():
            continue
        entry = by_name.get(node.name)
        if entry is None:
            skipped.append(node.name)
            continue
        address = settings.proxmox_host if entry.local else entry.ip
        if not address:
            skipped.append(node.name)
            continue
        node.address = address
        session.add(node)
        filled[node.name] = address
    if filled:
        audit.record(
            session,
            actor=admin.username,
            action="node.addresses_filled",
            target="nodes",
            details={"filled": filled},
        )
    session.commit()
    return {"filled": filled, "skipped": skipped}


@router.get("/nodes/status")
async def nodes_status(session: SessionDep, adapters: AdaptersDep) -> dict[str, Any]:
    """Live power state of every node: the power manager's report (when the
    node has a power target) and whether Proxmox sees it online. Read-only,
    never raises for one unreachable source - that source reads ``None``."""
    nodes = session.exec(select(Node).order_by(Node.name)).all()

    proxmox_error: str | None = None
    online: dict[str, bool] = {}
    try:
        cluster = await adapters.proxmox.cluster_status()
        online = {n.name: n.online for n in cluster.nodes}
    except Exception as exc:  # unreachable cluster: report it, keep the power view
        proxmox_error = str(exc)

    async def power_of(node: Node) -> tuple[PowerState | None, str | None]:
        if not node.power_mgr_target.strip():
            return None, "no power target"
        try:
            report = await asyncio.wait_for(
                adapters.power.get_status(node.power_mgr_target), _POWER_STATUS_TIMEOUT_S
            )
        except TimeoutError:
            return None, "power manager did not answer"
        except Exception as exc:
            return None, str(exc)
        return report.state, report.detail

    reports = await asyncio.gather(*(power_of(n) for n in nodes))
    healths = await asyncio.gather(*(node_health_view(adapters, n) for n in nodes))
    out = []
    for node, (power, detail), health in zip(nodes, reports, healths, strict=True):
        pve = online.get(node.name) if not proxmox_error else None
        out.append(
            {
                "name": node.name,
                "always_on": node.always_on,
                "address": node.address,
                "power": power.value if power else None,
                "power_detail": detail,
                "proxmox_online": pve,
                "health": health,
                "state": combined_node_state(power, pve, health.get("reachable")),
            }
        )
    return {"nodes": out, "proxmox_error": proxmox_error}


@router.post("/nodes/{name}/power")
async def node_power_action(
    name: str,
    body: NodePowerIn,
    session: SessionDep,
    adapters: AdaptersDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    """Manual, admin-triggered power action on a ``Node``, via
    ``adapters.power`` directly.

    Deliberately NOT the wake pipeline — no guest start, no health wait, no
    hold asserted. This is a genuinely destructive action for power_off/
    restart (cutting power to a physical/VM host); the dashboard button
    gates those two behind a ``confirm()`` dialog that spells out exactly
    what ``force=true`` means. ``wake`` is harmless and unconditional in the
    UI too.
    """
    return await apply_node_power(
        session, adapters, admin.username, name, body.action, force=body.force
    )


async def apply_node_power(
    session: Session, adapters: Any, actor: str, name: str, action: str, *, force: bool = False
) -> dict[str, Any]:
    """:func:`node_power_action`, callable (reused by ``POST /api/bulk/nodes``)."""
    node = session.exec(select(Node).where(Node.name == name)).one_or_none()
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no node named {name!r}")

    reason = f"manual dashboard action by {actor}"
    try:
        if action == "wake":
            await adapters.power.wake(node.power_mgr_target, reason=reason)
        elif action == "power_off":
            await adapters.power.power_off(node.power_mgr_target, reason=reason, force=force)
        elif action == "restart":
            await adapters.power.restart(node.power_mgr_target, reason=reason, force=force)
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown power action {action!r}")
    except AdapterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    audit.record(
        session,
        actor=actor,
        action="node.power_action",
        target=f"node:{node.name}",
        details={"action": action, "force": force},
    )
    session.commit()
    return {"status": "ok", "node": node.name, "action": action, "force": force}


def _service_for_guest(session: Session, node: str, vmid: int) -> Service | None:
    """The registered service running guest ``vmid`` on ``node``, if any."""
    row = session.exec(select(Node).where(Node.name == node)).one_or_none()
    if row is None:
        return None
    return session.exec(
        select(Service).where(Service.node_id == row.id, Service.guest_id == vmid)
    ).first()


@router.get("/proxmox/guests")
async def list_guests(
    adapters: AdaptersDep, session: SessionDep, node: str | None = None
) -> list[dict[str, Any]]:
    guests = await adapters.proxmox.list_guests(node)
    out = []
    for g in guests:
        svc = _service_for_guest(session, g.node, g.vmid)
        out.append(
            {
                "node": g.node,
                "vmid": g.vmid,
                "kind": g.kind.value,
                "name": g.name,
                "status": g.status,
                # Registered as a service? Power actions then go through the
                # service's own endpoint (and its mid-backup guardrail).
                "service_slug": svc.slug if svc else None,
            }
        )
    return out


class GuestPowerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop"]


@router.post("/guests/{node}/{vmid}/power")
async def guest_power_action(
    node: str,
    vmid: int,
    body: GuestPowerIn,
    session: SessionDep,
    adapters: AdaptersDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    """Raw start/stop of any Proxmox guest, registered or not - the guests
    page's Start/Stop. A guest that IS a registered service is handed to
    ``POST /api/services/{slug}/guest/power``'s code path instead, so its
    "never stop mid-backup" guardrail and audit entry still apply. Otherwise
    ``adapters.proxmox.start_guest``/``stop_guest`` straight through (graceful
    shutdown for stop), audited as ``infra.guest_power_action``."""
    from orchestrator.web.routers.dashboard import guest_power_for_service

    svc = _service_for_guest(session, node, vmid)
    if svc is not None and svc.guest_id == vmid and svc.guest_kind is not GuestKind.NONE:
        return await guest_power_for_service(
            session, adapters, admin.username, svc.slug, body.action
        )

    try:
        guests = await adapters.proxmox.list_guests(node)
    except AdapterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    guest = next((g for g in guests if g.vmid == vmid and g.node == node), None)
    if guest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no guest {vmid} on node {node!r}")
    try:
        if body.action == "start":
            handle = await adapters.proxmox.start_guest(node, vmid, guest.kind)
        else:
            handle = await adapters.proxmox.stop_guest(node, vmid, guest.kind)
    except AdapterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    audit.record(
        session,
        actor=admin.username,
        action="infra.guest_power_action",
        target=f"guest:{node}/{vmid}",
        details={
            "action": body.action,
            "node": node,
            "vmid": vmid,
            "guest_kind": guest.kind.value,
            "name": guest.name,
            "upid": handle.upid,
        },
    )
    session.commit()
    return {"status": "ok", "node": node, "vmid": vmid, "action": body.action, "upid": handle.upid}


@router.get("/pbs/snapshots")
async def list_snapshots(
    adapters: AdaptersDep,
    settings: SettingsDep,
    backup_type: str | None = None,
    backup_id: str | None = None,
) -> list[dict[str, Any]]:
    snapshots = await adapters.pbs.list_snapshots(
        settings.pbs_datastore, backup_type=backup_type, backup_id=backup_id
    )
    return [
        {
            "snapshot_id": s.snapshot_id,
            "group": s.group,
            "backup_time": s.backup_time,
            "size_bytes": s.size_bytes,
            "verified": s.verified,
            "protected": s.protected,
            "owner": s.owner,
        }
        for s in snapshots
    ]


class ResyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Only services on this node ...
    node: str | None = None
    #: ... or just this one. Neither = every enabled service.
    slug: str | None = None


@router.post("/resync")
async def resync_guests(
    body: ResyncIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    return await do_resync(
        session, adapters, settings, admin.username, node=body.node, slug=body.slug
    )


async def do_resync(
    session: Session,
    adapters: Any,
    settings: Any,
    actor: str,
    *,
    node: str | None = None,
    slug: str | None = None,
) -> dict[str, Any]:
    """Re-read the guests from Proxmox now: scan the selected services (the
    guest gate first, then their probes) so the dashboard shows current state
    instead of waiting for the next scheduled scan. Reports each service's
    guest state, any whose guest is no longer in Proxmox, and Proxmox guests no
    service points at."""
    from orchestrator.health.engine import HealthEngine
    from orchestrator.web.routers.ui import guest_gate_view

    if slug and node:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "pass a node or a slug, not both")
    stmt = select(Service).order_by(Service.slug)
    if slug:
        stmt = stmt.where(Service.slug == slug)
    elif node:
        row = session.exec(select(Node).where(Node.name == node)).one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no node named {node!r}")
        stmt = stmt.where(Service.node_id == row.id)
    services = list(session.exec(stmt).all())
    if slug and not services:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    tree = TreeIndex.build(session)
    services = [s for s in services if tree.is_served(s)]

    engine = HealthEngine(settings=settings, proxmox=adapters.proxmox)
    guest_cache = engine.new_guest_cache()
    cache: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    for svc in services:
        if svc.slug in cache:
            continue
        try:
            verdict = await engine.scan(session, svc, _cache=cache, _guest_cache=guest_cache)
        except Exception as exc:
            results.append({"slug": svc.slug, "ok": False, "message": str(exc)})
            continue
        state = session.get(ServiceState, svc.id)
        gate = guest_gate_view(state)
        results.append(
            {
                "slug": svc.slug,
                "ok": True,
                "verdict": verdict.state.value,
                "guest": gate["guest_state"] if gate else None,
                "missing": bool(gate and "was not found" in gate["message"]),
                "message": verdict.reason,
            }
        )

    registered = {
        (s.guest_id) for s in session.exec(select(Service)).all() if s.guest_id is not None
    }
    unregistered: list[dict[str, Any]] = []
    try:
        for g in await adapters.proxmox.list_guests(node):
            if g.vmid not in registered:
                unregistered.append(
                    {"node": g.node, "vmid": g.vmid, "name": g.name, "status": g.status}
                )
    except AdapterError:
        pass

    audit.record(
        session,
        actor=actor,
        action="infra.guests_resynced",
        target=f"node:{node}" if node else f"service:{slug}" if slug else "all",
        details={"services": len(results)},
    )
    session.commit()
    return {
        "scanned": len(results),
        "results": results,
        "missing": [r["slug"] for r in results if r.get("missing")],
        "unregistered_guests": unregistered,
    }
