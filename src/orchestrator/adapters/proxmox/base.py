"""Proxmox VE API adapter interface (guest list, VM lifecycle, vzdump)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from orchestrator.domain.enums import GuestKind


@dataclass(frozen=True)
class Guest:
    node: str
    vmid: int
    kind: GuestKind
    name: str
    status: str  # "running" | "stopped" | ...


@dataclass(frozen=True)
class TaskHandle:
    node: str
    upid: str  # Proxmox UPID string


@dataclass(frozen=True)
class TaskStatus:
    upid: str
    status: str  # "running" | "stopped"
    exit_status: str | None  # None while running; "OK" or an error string when done
    log_tail: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.status == "stopped"

    @property
    def succeeded(self) -> bool:
        """Proxmox reports success as the literal string ``OK``."""
        return self.finished and self.exit_status == "OK"


@dataclass(frozen=True)
class VersionInfo:
    version: str
    release: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClusterNode:
    name: str
    online: bool
    #: True for the node serving this API endpoint — the one PROXMOX_HOST reaches.
    local: bool = False
    ip: str = ""


@dataclass(frozen=True)
class ClusterStatus:
    nodes: list[ClusterNode]
    #: None on a standalone node, which has no quorum to lose.
    quorate: bool | None = None
    cluster_name: str = ""

    @property
    def local_node(self) -> ClusterNode | None:
        return next((n for n in self.nodes if n.local), None)


@dataclass(frozen=True)
class BackupStorage:
    """A PVE storage entry of type ``pbs`` — the name ``vzdump storage=`` takes."""

    storage: str
    datastore: str
    server: str


class ProxmoxAdapter(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def version(self) -> VersionInfo:
        """Cheap authenticated call — used as the connectivity check."""

    @abstractmethod
    async def list_guests(self, node: str | None = None) -> list[Guest]: ...

    @abstractmethod
    async def cluster_status(self) -> ClusterStatus:
        """Nodes, which one serves this endpoint, and whether the cluster is quorate."""

    @abstractmethod
    async def list_backup_storages(self) -> list[BackupStorage]:
        """PVE storage entries that point at a PBS datastore."""

    @abstractmethod
    async def start_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle: ...

    @abstractmethod
    async def stop_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle: ...

    @abstractmethod
    async def vzdump(
        self,
        *,
        node: str,
        vmid: int,
        storage: str,
        mode: str,  # "snapshot" | "suspend" | "stop"
        notes: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TaskHandle:
        """Kick a ``vzdump`` on ``node`` writing to the PVE storage ``storage``.

        Note ``storage`` is the **PVE storage ID** that points at PBS, which is
        not necessarily the same string as the PBS datastore name.
        """

    @abstractmethod
    async def task_status(self, node: str, upid: str) -> TaskStatus: ...

    @abstractmethod
    async def wait_for_task(
        self,
        node: str,
        upid: str,
        *,
        timeout_s: int = 3600,
        poll_interval_s: float = 3.0,
    ) -> TaskStatus:
        """Poll until the task finishes.

        Raises :class:`~orchestrator.adapters.errors.TaskTimeout` if it does not
        finish in time — the task may still be running server-side, so callers
        must treat that as indeterminate rather than as a failure.
        """
