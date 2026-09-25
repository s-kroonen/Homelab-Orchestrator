"""Log-only Proxmox adapter for tests, dev, and DRY_RUN mode."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

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
from orchestrator.domain.enums import GuestKind
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


def _fake_upid(node: str, worker: str = "dry_run") -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"UPID:{node}:{uuid.uuid4().hex[:8]}:{ts}:{worker}:"


class DryRunProxmoxAdapter(ProxmoxAdapter):
    """Records intent instead of acting.

    ``simulated_guests`` can be populated by tests so pipeline code has
    something to resolve against.
    """

    def __init__(self, simulated_guests: list[Guest] | None = None) -> None:
        self._tasks: dict[str, TaskStatus] = {}
        self.simulated_guests: list[Guest] = simulated_guests or []
        # Test hook: set to an exit status string to make the next task "fail".
        self.next_task_exit_status: str = "OK"
        #: Tests set these. Unset, the cluster is derived from simulated_guests.
        self.simulated_cluster: ClusterStatus | None = None
        self.simulated_backup_storages: list[BackupStorage] = []

    async def start(self) -> None:
        log.info("proxmox.dry_run.start")

    async def stop(self) -> None:
        log.info("proxmox.dry_run.stop")

    async def version(self) -> VersionInfo:
        return VersionInfo(version="dry-run", release="dry-run", raw={"dry_run": True})

    async def list_guests(self, node: str | None = None) -> list[Guest]:
        log.info("proxmox.dry_run.list_guests", node=node)
        if node is None:
            return list(self.simulated_guests)
        return [g for g in self.simulated_guests if g.node == node]

    async def cluster_status(self) -> ClusterStatus:
        log.info("proxmox.dry_run.cluster_status")
        if self.simulated_cluster is not None:
            return self.simulated_cluster
        names = sorted({g.node for g in self.simulated_guests})
        nodes = [ClusterNode(name=n, online=True, local=i == 0) for i, n in enumerate(names)]
        return ClusterStatus(nodes=nodes, quorate=True if len(nodes) > 1 else None)

    async def list_backup_storages(self) -> list[BackupStorage]:
        return list(self.simulated_backup_storages)

    async def start_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle:
        return self._record_task("proxmox.dry_run.start_guest", node, vmid=vmid, kind=kind.value)

    async def stop_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle:
        return self._record_task("proxmox.dry_run.stop_guest", node, vmid=vmid, kind=kind.value)

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
        return self._record_task(
            "proxmox.dry_run.vzdump",
            node,
            vmid=vmid,
            storage=storage,
            mode=mode,
            notes=notes,
            extra=extra or {},
            log_line=f"[dry_run] would vzdump vmid={vmid} to {storage} mode={mode}",
        )

    async def task_status(self, node: str, upid: str) -> TaskStatus:
        return self._tasks.get(
            upid,
            TaskStatus(upid=upid, status="stopped", exit_status="OK", log_tail=[]),
        )

    async def wait_for_task(
        self,
        node: str,
        upid: str,
        *,
        timeout_s: int = 3600,
        poll_interval_s: float = 3.0,
    ) -> TaskStatus:
        # Dry-run tasks are always already complete — no sleeping in tests.
        return await self.task_status(node, upid)

    def _record_task(
        self, event: str, node: str, *, log_line: str | None = None, **fields: Any
    ) -> TaskHandle:
        upid = _fake_upid(node)
        log.info(event, node=node, upid=upid, **fields)
        self._tasks[upid] = TaskStatus(
            upid=upid,
            status="stopped",
            exit_status=self.next_task_exit_status,
            log_tail=[log_line] if log_line else [],
        )
        return TaskHandle(node=node, upid=upid)
