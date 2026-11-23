"""Live Proxmox Backup Server API adapter."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from orchestrator.adapters.errors import TaskTimeout
from orchestrator.adapters.pbs.base import (
    DatastoreStatus,
    GcStatus,
    PbsAdapter,
    PbsTask,
    PbsVersionInfo,
    Snapshot,
)
from orchestrator.adapters.proxmox_family import ProxmoxFamilyClient
from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class LivePbsAdapter(PbsAdapter):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # PBS runs tasks under a node name; on a standalone PBS this is
        # "localhost" unless the install was renamed.
        self._node = settings.pbs_node_name
        self._client = ProxmoxFamilyClient(
            flavor="pbs",
            host=settings.pbs_host,
            port=settings.pbs_port,
            token_id=settings.pbs_token_id,
            token_secret=settings.pbs_token_secret.get_secret_value(),
            verify_tls=settings.pbs_verify_tls,
        )

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    async def version(self) -> PbsVersionInfo:
        data = await self._client.request("GET", "/version") or {}
        return PbsVersionInfo(
            version=str(data.get("version", "")),
            release=str(data.get("release", "")),
            raw=data,
        )

    async def effective_permissions(self) -> dict[str, Any]:
        data = await self._client.request("GET", "/access/permissions")
        return data or {}

    async def list_datastores(self) -> list[str]:
        rows = await self._client.request("GET", "/admin/datastore") or []
        return sorted(str(row["store"]) for row in rows if row.get("store"))

    async def datastore_status(self, name: str) -> DatastoreStatus:
        data = await self._client.request("GET", f"/admin/datastore/{name}/status") or {}
        return DatastoreStatus(
            name=name,
            total_bytes=int(data.get("total", 0)),
            used_bytes=int(data.get("used", 0)),
            available_bytes=int(data.get("avail", 0)),
            reachable=True,
        )

    async def list_snapshots(
        self,
        datastore: str,
        *,
        backup_type: str | None = None,
        backup_id: str | None = None,
    ) -> list[Snapshot]:
        rows = await self._client.request(
            "GET",
            f"/admin/datastore/{datastore}/snapshots",
            params={"backup-type": backup_type, "backup-id": backup_id},
        )
        snapshots = [_snapshot_from_row(datastore, row) for row in rows or []]
        # Newest first — every caller wants the latest.
        return sorted(snapshots, key=lambda s: s.backup_time, reverse=True)

    async def verify_snapshot(
        self,
        datastore: str,
        *,
        backup_type: str,
        backup_id: str,
        backup_time: int,
        timeout_s: int = 1800,
    ) -> bool:
        upid = await self._client.request(
            "POST",
            f"/admin/datastore/{datastore}/verify",
            data={
                "backup-type": backup_type,
                "backup-id": backup_id,
                "backup-time": backup_time,
            },
        )
        log.info(
            "pbs.verify.started",
            datastore=datastore,
            group=f"{backup_type}/{backup_id}",
            backup_time=backup_time,
            upid=upid,
        )
        status = await self._wait_for_task(str(upid), timeout_s=timeout_s)
        verified = status.get("exitstatus") == "OK"
        log.info(
            "pbs.verify.finished",
            upid=upid,
            verified=verified,
            exit_status=status.get("exitstatus"),
        )
        return verified

    async def prune(
        self,
        *,
        datastore: str,
        backup_type: str,
        backup_id: str,
        keep_daily: int | None = None,
        keep_monthly: int | None = None,
        keep_last: int | None = None,
        dry_run: bool = False,
    ) -> list[Snapshot]:
        rows = await self._client.request(
            "POST",
            f"/admin/datastore/{datastore}/prune",
            data={
                "backup-type": backup_type,
                "backup-id": backup_id,
                "keep-daily": keep_daily,
                "keep-monthly": keep_monthly,
                "keep-last": keep_last,
                "dry-run": 1 if dry_run else 0,
            },
        )
        # PBS returns every considered snapshot with a "keep" boolean; we only
        # care about the ones it removed (or would remove).
        removed = [
            _snapshot_from_row(datastore, row) for row in rows or [] if not row.get("keep", True)
        ]
        log.info(
            "pbs.prune",
            datastore=datastore,
            group=f"{backup_type}/{backup_id}",
            dry_run=dry_run,
            removed=len(removed),
        )
        return removed

    async def set_protected(
        self,
        datastore: str,
        *,
        backup_type: str,
        backup_id: str,
        backup_time: int,
        protected: bool,
    ) -> None:
        if not self._settings.pbs_protect_enabled:
            log.warning(
                "pbs.set_protected.disabled",
                datastore=datastore,
                group=f"{backup_type}/{backup_id}",
                backup_time=backup_time,
                requested=protected,
                reason="PBS_PROTECT_ENABLED=false (token lacks Datastore.Modify)",
                impact="This snapshot is NOT pinned; prune may remove it.",
            )
            return

        await self._client.request(
            "PUT",
            f"/admin/datastore/{datastore}/protected",
            data={
                "backup-type": backup_type,
                "backup-id": backup_id,
                "backup-time": backup_time,
                "protected": 1 if protected else 0,
            },
        )
        log.info(
            "pbs.set_protected",
            datastore=datastore,
            group=f"{backup_type}/{backup_id}",
            backup_time=backup_time,
            protected=protected,
        )

    async def start_garbage_collection(self, datastore: str) -> str:
        upid = await self._client.request("POST", f"/admin/datastore/{datastore}/gc")
        log.info("pbs.gc.started", datastore=datastore, upid=upid)
        return str(upid)

    async def garbage_collection_status(self, datastore: str) -> GcStatus:
        data = await self._client.request("GET", f"/admin/datastore/{datastore}/gc") or {}

        def num(key: str) -> int | None:
            value = data.get(key)
            return int(value) if isinstance(value, int | float) else None

        return GcStatus(
            upid=str(data["upid"]) if data.get("upid") else None,
            removed_bytes=num("removed-bytes"),
            pending_bytes=num("pending-bytes"),
            disk_bytes=num("disk-bytes"),
            index_file_count=num("index-file-count"),
            raw=dict(data),
        )

    async def task(self, upid: str, *, log_lines: int = 50) -> PbsTask:
        node = _task_node(upid, self._node)
        status = await self._client.request("GET", f"/nodes/{node}/tasks/{upid}/status") or {}
        running = status.get("status") != "stopped"
        tail: list[str] = []
        try:
            # The log comes from its first line; read enough and keep the end.
            rows = await self._client.request(
                "GET", f"/nodes/{node}/tasks/{upid}/log", params={"limit": 100000}
            )
            tail = [str(r.get("t", "")) for r in rows or []][-log_lines:]
        except Exception as exc:  # the log is a nicety; never fail the poll over it
            log.debug("pbs.task_log.unavailable", upid=upid, error=str(exc))
        return PbsTask(
            upid=upid,
            running=running,
            exit_status=None if running else status.get("exitstatus"),
            log_tail=tail,
        )

    async def stop_task(self, upid: str) -> None:
        node = _task_node(upid, self._node)
        await self._client.request("DELETE", f"/nodes/{node}/tasks/{upid}")
        log.warning("pbs.task.stopped", upid=upid)

    async def delete_snapshot(
        self, datastore: str, *, backup_type: str, backup_id: str, backup_time: int
    ) -> None:
        await self._client.request(
            "DELETE",
            f"/admin/datastore/{datastore}/snapshots",
            params={"backup-type": backup_type, "backup-id": backup_id, "backup-time": backup_time},
        )
        log.warning(
            "pbs.snapshot.deleted",
            datastore=datastore,
            group=f"{backup_type}/{backup_id}",
            backup_time=backup_time,
        )

    async def own_jobs(self, datastore: str) -> list[dict[str, Any]]:
        """Best effort, version by version: a list endpoint PBS does not have (older PBS) or
        the token may not read is left out rather than failing the whole answer."""
        jobs: list[dict[str, Any]] = []

        def job(kind: str, row: dict[str, Any], detail: str = "") -> dict[str, Any]:
            return {
                "kind": kind,
                "id": str(row.get("id") or row.get("store") or kind),
                "schedule": row.get("schedule") or row.get("gc-schedule") or "",
                "last_state": row.get("last-run-state"),
                "last_end": row.get("last-run-endtime"),
                "next_run": row.get("next-run"),
                "detail": detail,
            }

        gc_found = False
        try:
            for row in await self._client.request("GET", "/admin/gc") or []:
                if row.get("store") == datastore:
                    jobs.append(job("gc", row))
                    gc_found = True
        except Exception as exc:
            log.debug("pbs.own_jobs.gc_unavailable", error=str(exc))
        if not gc_found:
            try:
                cfg = await self._client.request("GET", f"/config/datastore/{datastore}") or {}
                if cfg.get("gc-schedule"):
                    jobs.append(job("gc", {"schedule": cfg["gc-schedule"], "id": datastore}))
            except Exception as exc:
                log.debug("pbs.own_jobs.config_unavailable", error=str(exc))
        for kind, path in (("prune", "/admin/prune"), ("verify", "/admin/verify")):
            try:
                rows = await self._client.request("GET", path) or []
            except Exception as exc:
                log.debug("pbs.own_jobs.unavailable", kind=kind, error=str(exc))
                continue
            for row in rows:
                if row.get("store") != datastore:
                    continue
                keeps = ", ".join(
                    f"{k}={v}" for k, v in sorted(row.items()) if k.startswith("keep-") and v
                )
                jobs.append(job(kind, row, keeps))
        return jobs

    async def _wait_for_task(
        self, upid: str, *, timeout_s: int, poll_interval_s: float = 3.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            status = (
                await self._client.request("GET", f"/nodes/{self._node}/tasks/{upid}/status") or {}
            )
            if status.get("status") == "stopped":
                return status
            if time.monotonic() >= deadline:
                raise TaskTimeout(f"PBS task {upid} still running after {timeout_s}s", upid=upid)
            await asyncio.sleep(poll_interval_s)


def _task_node(upid: str, default: str) -> str:
    """The node a UPID belongs to (``UPID:<node>:...``); PBS names it in the UPID."""
    parts = upid.split(":")
    return parts[1] if len(parts) > 2 and parts[0] == "UPID" and parts[1] else default


def _snapshot_from_row(datastore: str, row: dict[str, Any]) -> Snapshot:
    """Map one PBS snapshot row onto :class:`Snapshot`.

    ``verification`` is absent on never-verified snapshots and is a dict like
    ``{"state": "ok"}`` once a verify job has run.
    """
    verification = row.get("verification") or {}
    return Snapshot(
        datastore=datastore,
        backup_type=str(row.get("backup-type", "")),
        backup_id=str(row.get("backup-id", "")),
        backup_time=int(row.get("backup-time", 0)),
        size_bytes=int(row["size"]) if row.get("size") is not None else None,
        verified=verification.get("state") == "ok",
        protected=bool(row.get("protected", False)),
        owner=row.get("owner"),
    )
