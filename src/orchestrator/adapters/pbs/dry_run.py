"""Log-only PBS adapter for tests, dev, and DRY_RUN mode."""

from __future__ import annotations

from datetime import UTC, datetime

from orchestrator.adapters.pbs.base import (
    DatastoreStatus,
    PbsAdapter,
    PbsVersionInfo,
    Snapshot,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class DryRunPbsAdapter(PbsAdapter):
    """Records intent instead of acting.

    ``simulated_snapshots`` lets tests (and a dry-run backup pipeline) resolve
    a plausible snapshot after a simulated vzdump.

    ``synthesize_snapshots`` (default on) fabricates a snapshot when a lookup
    finds nothing, so a dry-run backup can complete end-to-end instead of
    dead-ending at "vzdump succeeded but PBS is empty" — which is true of a
    fake PBS but useless as a rehearsal. Tests that exercise the genuinely
    missing-snapshot path turn it off explicitly.
    """

    def __init__(
        self,
        simulated_snapshots: list[Snapshot] | None = None,
        *,
        synthesize_snapshots: bool = True,
    ) -> None:
        self.simulated_snapshots: list[Snapshot] = simulated_snapshots or []
        self.synthesize_snapshots = synthesize_snapshots
        self.verify_result = True

    async def start(self) -> None:
        log.info("pbs.dry_run.start")

    async def stop(self) -> None:
        log.info("pbs.dry_run.stop")

    async def version(self) -> PbsVersionInfo:
        return PbsVersionInfo(version="dry-run", release="dry-run", raw={"dry_run": True})

    async def datastore_status(self, name: str) -> DatastoreStatus:
        log.info("pbs.dry_run.datastore_status", datastore=name)
        return DatastoreStatus(
            name=name,
            total_bytes=0,
            used_bytes=0,
            available_bytes=0,
            reachable=True,
        )

    async def list_snapshots(
        self,
        datastore: str,
        *,
        backup_type: str | None = None,
        backup_id: str | None = None,
    ) -> list[Snapshot]:
        log.info(
            "pbs.dry_run.list_snapshots",
            datastore=datastore,
            backup_type=backup_type,
            backup_id=backup_id,
        )
        matches = [
            s
            for s in self.simulated_snapshots
            if (backup_type is None or s.backup_type == backup_type)
            and (backup_id is None or s.backup_id == backup_id)
        ]
        if not matches and self.synthesize_snapshots and backup_type and backup_id:
            synthetic = Snapshot(
                datastore=datastore,
                backup_type=backup_type,
                backup_id=backup_id,
                backup_time=int(datetime.now(UTC).timestamp()),
                size_bytes=0,
                verified=False,
                protected=False,
                owner="dry-run",
            )
            log.info("pbs.dry_run.synthesized_snapshot", snapshot=synthetic.snapshot_id)
            return [synthetic]
        return sorted(matches, key=lambda s: s.backup_time, reverse=True)

    async def verify_snapshot(
        self,
        datastore: str,
        *,
        backup_type: str,
        backup_id: str,
        backup_time: int,
        timeout_s: int = 1800,
    ) -> bool:
        log.info(
            "pbs.dry_run.verify_snapshot",
            datastore=datastore,
            group=f"{backup_type}/{backup_id}",
            backup_time=backup_time,
            result=self.verify_result,
        )
        return self.verify_result

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
        log.info(
            "pbs.dry_run.prune",
            datastore=datastore,
            group=f"{backup_type}/{backup_id}",
            keep_daily=keep_daily,
            keep_monthly=keep_monthly,
            keep_last=keep_last,
            dry_run=dry_run,
        )
        return []

    async def set_protected(
        self,
        datastore: str,
        *,
        backup_type: str,
        backup_id: str,
        backup_time: int,
        protected: bool,
    ) -> None:
        log.info(
            "pbs.dry_run.set_protected",
            datastore=datastore,
            group=f"{backup_type}/{backup_id}",
            backup_time=backup_time,
            protected=protected,
        )
