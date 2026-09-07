"""Live Proxmox Backup Server API adapter."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from orchestrator.adapters.errors import TaskTimeout
from orchestrator.adapters.pbs.base import (
    DatastoreStatus,
    PbsAdapter,
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
