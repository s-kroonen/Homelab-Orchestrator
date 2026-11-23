"""Proxmox Backup Server API adapter interface.

PBS addresses a snapshot by the triple ``(backup-type, backup-id, backup-time)``
where ``backup-time`` is a **unix epoch integer**.  :class:`Snapshot` stores it
that way so it round-trips into verify/prune/protect calls without reparsing,
and exposes :attr:`Snapshot.snapshot_id` for the human/display form that
``BackupRecord.pbs_snapshot_id`` persists.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Snapshot:
    datastore: str
    backup_type: str  # "vm" | "ct" | "host"
    backup_id: str  # e.g. "9001"
    backup_time: int  # unix epoch seconds — what the PBS API expects
    size_bytes: int | None = None
    verified: bool = False
    protected: bool = False
    owner: str | None = None

    @property
    def snapshot_id(self) -> str:
        """Display/persistence form, e.g. ``vm/9001/2026-08-24T02:00:00Z``."""
        ts = datetime.fromtimestamp(self.backup_time, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{self.backup_type}/{self.backup_id}/{ts}"

    @property
    def group(self) -> str:
        """PBS backup group, e.g. ``vm/9001``."""
        return f"{self.backup_type}/{self.backup_id}"


@dataclass(frozen=True)
class DatastoreStatus:
    name: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    reachable: bool

    @property
    def used_fraction(self) -> float:
        return (self.used_bytes / self.total_bytes) if self.total_bytes else 0.0


@dataclass(frozen=True)
class GcStatus:
    """What PBS says about a datastore's garbage collection: the last one's numbers, and
    the task (``upid``) of the one running or last run. Missing numbers are None."""

    upid: str | None = None
    removed_bytes: int | None = None
    pending_bytes: int | None = None
    disk_bytes: int | None = None
    index_file_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PbsTask:
    """One PBS task (a GC, a verify, a prune job): running or stopped, and the end of its log."""

    upid: str
    running: bool
    exit_status: str | None = None
    log_tail: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.running and self.exit_status == "OK"


@dataclass(frozen=True)
class PbsVersionInfo:
    version: str
    release: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class PbsAdapter(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def version(self) -> PbsVersionInfo:
        """Cheap authenticated call — used as the connectivity check."""

    @abstractmethod
    async def effective_permissions(self) -> dict[str, Any]:
        """What this token can actually do, per PBS itself.

        An empty mapping means the token has NO permissions — which happens when
        the ACL was granted to the *user* but not to the token auth-id. That is
        the single most common PBS misconfiguration and is otherwise only
        visible as an opaque 403 on the first real call.
        """

    @abstractmethod
    async def list_datastores(self) -> list[str]:
        """Datastore names this token can see.

        PBS filters the list by Datastore.Audit rather than refusing, so an empty
        list usually means a missing ACL rather than a PBS with no datastores.
        """

    @abstractmethod
    async def datastore_status(self, name: str) -> DatastoreStatus: ...

    @abstractmethod
    async def list_snapshots(
        self,
        datastore: str,
        *,
        backup_type: str | None = None,
        backup_id: str | None = None,
    ) -> list[Snapshot]: ...

    @abstractmethod
    async def verify_snapshot(
        self,
        datastore: str,
        *,
        backup_type: str,
        backup_id: str,
        backup_time: int,
        timeout_s: int = 1800,
    ) -> bool:
        """Run a PBS verify job and block until it finishes. True if verified."""

    @abstractmethod
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
        """Return the snapshots that would be / were removed."""

    # -- garbage collection and tasks (docs/cleanup.md) ---------------------------------

    async def start_garbage_collection(self, datastore: str) -> str:
        """Start PBS's garbage collection on ``datastore``; return its task's UPID."""
        raise NotImplementedError

    async def garbage_collection_status(self, datastore: str) -> GcStatus:
        """The last (or running) garbage collection of ``datastore``."""
        raise NotImplementedError

    async def task(self, upid: str, *, log_lines: int = 50) -> PbsTask:
        """One task: running or not, its exit status, the last ``log_lines`` of its log."""
        raise NotImplementedError

    async def stop_task(self, upid: str) -> None:
        """Ask PBS to stop a running task."""
        raise NotImplementedError

    @abstractmethod
    async def set_protected(
        self,
        datastore: str,
        *,
        backup_type: str,
        backup_id: str,
        backup_time: int,
        protected: bool,
    ) -> None:
        """Pin (or unpin) a snapshot so prune can never remove it."""
