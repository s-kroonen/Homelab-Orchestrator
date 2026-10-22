"""Scheduled backups of the orchestrator's own state (database + services.yaml)."""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger
from orchestrator.system.backups import BackupError, create_backup, prune_backups

log = get_logger(__name__)

_JOB_ID = "self-backup"


class SelfBackupScheduler:
    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        if not self._settings.self_backup_enabled:
            log.info("self_backup.disabled", reason="SELF_BACKUP_ENABLED=false")
            return
        hours = max(1, self._settings.self_backup_interval_hours)
        self._scheduler.start()
        self._scheduler.add_job(
            self.run_once,
            "interval",
            hours=hours,
            id=_JOB_ID,
            name="orchestrator self-backup",
            # Default first run = one interval from now, so a crash loop cannot
            # fill the disk with backups.
            max_instances=1,
            coalesce=True,
        )
        log.info("self_backup.started", every_hours=hours, keep=self._settings.self_backup_keep)

    async def run_once(self) -> None:
        """Never raises: a failed backup is logged and the next one tries again."""
        try:
            info = await asyncio.to_thread(create_backup, self._settings, reason="scheduled")
            gone = await asyncio.to_thread(prune_backups, self._settings)
            log.info("self_backup.done", name=info.name, pruned=len(gone))
        except BackupError as exc:
            log.warning("self_backup.skipped", error=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.error("self_backup.failed", error=str(exc), error_type=type(exc).__name__)

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
