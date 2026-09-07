"""Read-only infrastructure inspection — connectivity checks, guests, snapshots.

Deliberately separate from ``/healthz``: those report on *us*, these report on
what we can see of Proxmox and PBS. An unreachable Proxmox is not an
orchestrator health problem.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable
from orchestrator.web.deps import AdaptersDep, SettingsDep

router = APIRouter(prefix="/api/infra", tags=["infra"])


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
