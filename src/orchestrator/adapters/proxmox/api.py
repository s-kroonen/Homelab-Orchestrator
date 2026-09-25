"""Live Proxmox VE API adapter."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from orchestrator.adapters.errors import TaskTimeout
from orchestrator.adapters.proxmox.base import (
    BackupStorage,
    ClusterNode,
    ClusterStatus,
    Guest,
    ProxmoxAdapter,
    TaskHandle,
    TaskStatus,
    VersionInfo,
)
from orchestrator.adapters.proxmox_family import ProxmoxFamilyClient
from orchestrator.config import Settings
from orchestrator.domain.enums import GuestKind
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

# Proxmox calls VMs "qemu" and containers "lxc" in its URL space.
_KIND_TO_ENDPOINT: dict[GuestKind, str] = {
    GuestKind.VM: "qemu",
    GuestKind.CT: "lxc",
}
_TYPE_TO_KIND: dict[str, GuestKind] = {
    "qemu": GuestKind.VM,
    "lxc": GuestKind.CT,
}


class LiveProxmoxAdapter(ProxmoxAdapter):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = ProxmoxFamilyClient(
            flavor="pve",
            host=settings.proxmox_host,
            port=settings.proxmox_port,
            token_id=settings.proxmox_token_id,
            token_secret=settings.proxmox_token_secret.get_secret_value(),
            verify_tls=settings.proxmox_verify_tls,
        )

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    async def version(self) -> VersionInfo:
        data = await self._client.request("GET", "/version")
        data = data or {}
        return VersionInfo(
            version=str(data.get("version", "")),
            release=str(data.get("release", "")),
            raw=data,
        )

    async def list_guests(self, node: str | None = None) -> list[Guest]:
        rows = await self._client.request("GET", "/cluster/resources", params={"type": "vm"})
        guests: list[Guest] = []
        for row in rows or []:
            kind = _TYPE_TO_KIND.get(str(row.get("type", "")))
            if kind is None:
                continue
            if node is not None and row.get("node") != node:
                continue
            guests.append(
                Guest(
                    node=str(row.get("node", "")),
                    vmid=int(row.get("vmid", 0)),
                    kind=kind,
                    name=str(row.get("name", "")),
                    status=str(row.get("status", "unknown")),
                )
            )
        return sorted(guests, key=lambda g: g.vmid)

    async def cluster_status(self) -> ClusterStatus:
        rows = await self._client.request("GET", "/cluster/status") or []
        nodes: list[ClusterNode] = []
        quorate: bool | None = None
        cluster_name = ""
        for row in rows:
            if row.get("type") == "cluster":
                quorate = bool(row.get("quorate"))
                cluster_name = str(row.get("name", ""))
            elif row.get("type") == "node":
                nodes.append(
                    ClusterNode(
                        name=str(row.get("name", "")),
                        online=bool(row.get("online")),
                        local=bool(row.get("local")),
                        ip=str(row.get("ip", "")),
                    )
                )
        return ClusterStatus(
            nodes=sorted(nodes, key=lambda n: n.name),
            quorate=quorate,
            cluster_name=cluster_name,
        )

    async def list_backup_storages(self) -> list[BackupStorage]:
        rows = await self._client.request("GET", "/storage", params={"type": "pbs"}) or []
        storages = [
            BackupStorage(
                storage=str(row.get("storage", "")),
                datastore=str(row.get("datastore", "")),
                server=str(row.get("server", "")),
            )
            for row in rows
            if row.get("type", "pbs") == "pbs"
        ]
        return sorted(storages, key=lambda s: s.storage)

    async def start_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle:
        endpoint = self._endpoint_for(kind)
        upid = await self._client.request("POST", f"/nodes/{node}/{endpoint}/{vmid}/status/start")
        log.info("proxmox.start_guest", node=node, vmid=vmid, upid=upid)
        return TaskHandle(node=node, upid=str(upid))

    async def stop_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle:
        endpoint = self._endpoint_for(kind)
        upid = await self._client.request(
            "POST", f"/nodes/{node}/{endpoint}/{vmid}/status/shutdown"
        )
        log.info("proxmox.stop_guest", node=node, vmid=vmid, upid=upid)
        return TaskHandle(node=node, upid=str(upid))

    async def vzdump(
        self,
        *,
        node: str,
        vmid: int,
        storage: str,
        mode: str,
        notes: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TaskHandle:
        payload: dict[str, Any] = {
            "vmid": vmid,
            "storage": storage,
            "mode": mode,
        }
        if notes:
            # PBS shows this on the snapshot; makes orchestrator-made backups
            # identifiable in the PBS UI.
            payload["notes-template"] = notes
        if extra:
            payload.update(extra)

        upid = await self._client.request("POST", f"/nodes/{node}/vzdump", data=payload)
        log.info(
            "proxmox.vzdump.started",
            node=node,
            vmid=vmid,
            storage=storage,
            mode=mode,
            upid=upid,
        )
        return TaskHandle(node=node, upid=str(upid))

    async def task_status(self, node: str, upid: str) -> TaskStatus:
        status = await self._client.request("GET", f"/nodes/{node}/tasks/{upid}/status")
        status = status or {}

        log_tail: list[str] = []
        try:
            entries = await self._client.request(
                "GET", f"/nodes/{node}/tasks/{upid}/log", params={"limit": 50}
            )
            log_tail = [str(e.get("t", "")) for e in entries or []]
        except Exception as exc:  # log is a nicety — never fail the poll over it
            log.debug("proxmox.task_log.unavailable", upid=upid, error=str(exc))

        return TaskStatus(
            upid=upid,
            status=str(status.get("status", "unknown")),
            exit_status=status.get("exitstatus"),
            log_tail=log_tail,
        )

    async def wait_for_task(
        self,
        node: str,
        upid: str,
        *,
        timeout_s: int = 3600,
        poll_interval_s: float = 3.0,
    ) -> TaskStatus:
        deadline = time.monotonic() + timeout_s
        while True:
            status = await self.task_status(node, upid)
            if status.finished:
                log.info(
                    "proxmox.task.finished",
                    upid=upid,
                    exit_status=status.exit_status,
                    succeeded=status.succeeded,
                )
                return status
            if time.monotonic() >= deadline:
                raise TaskTimeout(f"task {upid} still running after {timeout_s}s", upid=upid)
            await asyncio.sleep(poll_interval_s)

    @staticmethod
    def _endpoint_for(kind: GuestKind) -> str:
        endpoint = _KIND_TO_ENDPOINT.get(kind)
        if endpoint is None:
            raise ValueError(f"guest kind {kind!r} has no Proxmox lifecycle endpoint")
        return endpoint
