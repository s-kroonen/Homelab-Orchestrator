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
from orchestrator.db.models import Node, Service
from orchestrator.domain.enums import GuestKind, PowerState
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
        "checks": [pve, pbs, datastore, *await power_checks(session, adapters)],
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


def combined_node_state(power: PowerState | None, proxmox_online: bool | None) -> str:
    """One word for the dashboard: ``on`` / ``off`` / ``booting`` /
    ``stopping`` / ``unknown``. Proxmox seeing the node online wins (it is
    serving the API right now); otherwise the power manager's own view; a
    node Proxmox reports offline with no power-manager answer counts as off."""
    if proxmox_online:
        return "on"
    if power in (PowerState.ON, PowerState.OFF, PowerState.BOOTING, PowerState.STOPPING):
        return power.value
    if proxmox_online is False:
        return "off"
    return "unknown"


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
    out = []
    for node, (power, detail) in zip(nodes, reports, strict=True):
        pve = online.get(node.name) if not proxmox_error else None
        out.append(
            {
                "name": node.name,
                "always_on": node.always_on,
                "power": power.value if power else None,
                "power_detail": detail,
                "proxmox_online": pve,
                "state": combined_node_state(power, pve),
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
