"""docker / systemd / any command, run through a transport (SSH, the host runner).

``{type: docker, container: nginx, transport: {type: host_agent, host: media-vm}}``
``{type: systemd, unit: nginx, transport: {...}}``
``{type: command, start: [...], stop: [...], restart: [...], status: [...], transport: {...}}``

Commands are argv lists, never shell strings.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import Settings
from orchestrator.controllers.base import (
    ControllerError,
    ControllerStatus,
    ControllerUnavailable,
    ServiceController,
)
from orchestrator.health.transports.base import (
    CommandTransport,
    TransportConfigError,
    TransportUnreachable,
)


def _argvs(cfg: dict[str, Any]) -> dict[str, list[str]]:
    kind = cfg["type"]
    if kind == "docker":
        c = str(cfg["container"])
        return {
            "start": ["docker", "start", c],
            "stop": ["docker", "stop", c],
            "restart": ["docker", "restart", c],
            "status": ["docker", "inspect", "-f", "{{.State.Status}}", c],
        }
    if kind == "systemd":
        u = str(cfg["unit"])
        return {
            "start": ["systemctl", "start", u],
            "stop": ["systemctl", "stop", u],
            "restart": ["systemctl", "restart", u],
            "status": ["systemctl", "is-active", u],
        }
    return {
        k: [str(a) for a in cfg[k]] for k in ("start", "stop", "restart", "status") if cfg.get(k)
    }


class CommandController(ServiceController):
    def __init__(
        self, settings: Settings, cfg: dict[str, Any], transport: CommandTransport
    ) -> None:
        self.type = str(cfg["type"])
        self._cfg = cfg
        self._argv = _argvs(cfg)
        self._transport = transport
        self._timeout = settings.controller_timeout_s

    def describe(self) -> str:
        what = self._cfg.get("container") or self._cfg.get("unit") or "custom command"
        return f"{self.type} {what}"

    async def _run(self, verb: str) -> tuple[int, str]:
        argv = self._argv.get(verb)
        if not argv:
            raise ControllerError(f"no {verb} command is configured")
        try:
            result = await self._transport.run(argv, timeout_s=self._timeout)
        except (TransportUnreachable, TransportConfigError) as exc:
            raise ControllerUnavailable(f"could not run {verb}: {exc}") from exc
        return result.exit_code, result.tail() or result.stdout.strip()

    async def status(self) -> ControllerStatus:
        code, out = await self._run("status")
        word = out.splitlines()[0].strip() if out else ("active" if code == 0 else "inactive")
        running = word in {"running", "active"} if self.type != "command" else code == 0
        return ControllerStatus(running, word)

    async def _act(self, verb: str) -> str:
        code, out = await self._run(verb)
        if code != 0:
            raise ControllerError(f"{verb} failed (exit {code}): {out}")
        return f"{verb} done ({self.describe()})"

    async def start(self) -> str:
        return await self._act("start")

    async def stop(self) -> str:
        return await self._act("stop")

    async def restart(self) -> str:
        return await self._act("restart")
