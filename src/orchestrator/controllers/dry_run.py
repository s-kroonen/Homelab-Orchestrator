"""Log-only controller for DRY_RUN and tests: remembers what it was asked."""

from __future__ import annotations

from typing import Any

from orchestrator.controllers.base import ControllerStatus, ServiceController
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class DryRunController(ServiceController):
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.type = str(cfg.get("type", "dry_run"))
        self._cfg = cfg
        self.calls: list[str] = []
        self._running = True

    def describe(self) -> str:
        return f"{self.type} (dry run)"

    async def status(self) -> ControllerStatus:
        return ControllerStatus(self._running, "running" if self._running else "stopped", "dry_run")

    async def start(self) -> str:
        self.calls.append("start")
        self._running = True
        log.info("controller.dry_run", action="start", cfg=self._cfg)
        return "start (dry run - nothing was started)"

    async def stop(self) -> str:
        self.calls.append("stop")
        self._running = False
        log.info("controller.dry_run", action="stop", cfg=self._cfg)
        return "stop (dry run - nothing was stopped)"

    async def restart(self) -> str:
        self.calls.append("restart")
        log.info("controller.dry_run", action="restart", cfg=self._cfg)
        return "restart (dry run - nothing was restarted)"
