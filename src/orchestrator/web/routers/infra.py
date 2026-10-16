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

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable
from orchestrator.audit import log as audit
from orchestrator.db.models import Node
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


@router.get("/status")
async def infra_status(adapters: AdaptersDep, settings: SettingsDep) -> dict[str, Any]:
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
        "checks": [pve, pbs, datastore],
    }


class NodePowerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["wake", "power_off", "restart"]
    #: Only meaningful for power_off/restart, matching ``PowerAdapter``'s own
    #: semantics exactly (see ``orchestrator/adapters/power/base.py``):
    #: False (default) asks for a graceful action, True skips straight to an
    #: immediate power cut / full power-cycle. Ignored for "wake".
    force: bool = False


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
    node = session.exec(select(Node).where(Node.name == name)).one_or_none()
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no node named {name!r}")

    reason = f"manual dashboard action by {admin.username}"
    try:
        if body.action == "wake":
            await adapters.power.wake(node.power_mgr_target, reason=reason)
        elif body.action == "power_off":
            await adapters.power.power_off(node.power_mgr_target, reason=reason, force=body.force)
        else:
            await adapters.power.restart(node.power_mgr_target, reason=reason, force=body.force)
    except AdapterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    audit.record(
        session,
        actor=admin.username,
        action="node.power_action",
        target=f"node:{node.name}",
        details={"action": body.action, "force": body.force},
    )
    session.commit()
    return {"status": "ok", "node": node.name, "action": body.action, "force": body.force}


@router.get("/proxmox/guests")
async def list_guests(adapters: AdaptersDep, node: str | None = None) -> list[dict[str, Any]]:
    guests = await adapters.proxmox.list_guests(node)
    return [
        {
            "node": g.node,
            "vmid": g.vmid,
            "kind": g.kind.value,
            "name": g.name,
            "status": g.status,
        }
        for g in guests
    ]


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
