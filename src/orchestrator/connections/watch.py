"""Notice when ``connections.yaml`` is edited outside the dashboard (the CLI, an editor, a copy
from the other site) and apply it, so no change needs a restart."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from orchestrator.connections import runtime
from orchestrator.logging_setup import get_logger

if TYPE_CHECKING:
    from orchestrator.adapters.factory import AdapterBundle

log = get_logger(__name__)
INTERVAL_S = 5.0


class ConnectionsWatcher:
    def __init__(self, adapters: AdapterBundle, interval_s: float = INTERVAL_S) -> None:
        self._adapters = adapters
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="connections-watcher")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def check_once(self) -> bool:
        """Apply the file if it changed since it was last applied. True if it was reloaded."""
        snap = runtime.current()
        if runtime.file_stamp(snap.path) == snap.stamp:
            return False
        from orchestrator.config import reload_settings

        settings = reload_settings()
        await self._adapters.reload(settings)
        after = runtime.current()
        log.info("connections.reloaded", path=str(after.path), error=after.error or None)
        return True

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("connections.reload_failed", error=str(exc))
