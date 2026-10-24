"""Updating and restarting the orchestrator itself."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from dataclasses import dataclass, field
from typing import Any

from orchestrator.config import Settings
from orchestrator.health.transports.base import TransportError
from orchestrator.logging_setup import get_logger
from orchestrator.system import selfupdate
from orchestrator.system.backups import BackupError, BackupInfo, create_backup

log = get_logger(__name__)

MANUAL_STEPS = [
    "docker compose pull orchestrator",
    "docker compose up -d orchestrator",
]


@dataclass
class UpdateResult:
    backup: BackupInfo | None = None
    backup_error: str | None = None
    ran: bool = False
    ok: bool = False
    exit_code: int | None = None
    output: str = ""
    message: str = ""
    manual_steps: list[str] = field(default_factory=list)
    #: "updater" (the compose sidecar), "command" (SELF_UPDATE_COMMAND) or "manual".
    method: str = "manual"
    request_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup": self.backup.as_dict() if self.backup else None,
            "backup_error": self.backup_error,
            "ran": self.ran,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "output": self.output,
            "message": self.message,
            "manual_steps": self.manual_steps,
            "method": self.method,
            "request_id": self.request_id,
        }


def _build_transport(settings: Settings) -> Any:
    from orchestrator.health.engine import TransportFactory
    from orchestrator.ipdiscovery.discovery import parse_transport_setting

    spec = parse_transport_setting(settings.self_update_transport)
    if spec is None:
        raise TransportError("SELF_UPDATE_TRANSPORT is not set")
    return TransportFactory(settings).build(spec)


def update_argv(command: str) -> list[str]:
    """The command as argv: split like a shell would (quotes honoured), never
    handed to a shell by us. Wrap it in ``sh -c '...'`` yourself if you need one."""
    return shlex.split(command)


async def run_update(settings: Settings, *, by: str = "dashboard") -> UpdateResult:
    """Back up first, then hand the update to the updater sidecar (which checks the new
    version and rolls back if it fails) or, without one, run the configured command.

    The container is normally replaced while the command runs, so the command
    should detach (see ``SELF_UPDATE_COMMAND`` in docs/system.md); the caller
    then waits for the new version to answer."""
    result = UpdateResult(manual_steps=list(MANUAL_STEPS))
    try:
        result.backup = create_backup(settings, reason="pre-update")
    except BackupError as exc:
        result.backup_error = str(exc)
        log.warning("system.update.backup_failed", error=str(exc))

    if selfupdate.updater_online(settings):
        try:
            result.request_id = selfupdate.request_update(
                settings, by=by, backup=result.backup.name if result.backup else None
            )
        except selfupdate.UpdaterError as exc:
            result.message = str(exc)
            return result
        result.method, result.ran, result.ok = "updater", True, True
        result.message = (
            "Update handed to the updater: it pulls the new build, restarts the orchestrator "
            "and puts the old version back if the new one does not come up healthy."
            + (
                ""
                if result.backup
                else " No backup could be taken, so a rollback restores the old build only."
            )
        )
        return result

    command = settings.self_update_command.strip()
    result.method = "command" if command else "manual"
    if not command:
        result.message = (
            "Updating from the dashboard is not set up: start the updater "
            "(docker compose --profile updater up -d) or set SELF_UPDATE_COMMAND. "
            "Run these on the Docker host instead."
        )
        return result
    try:
        transport = _build_transport(settings)
        argv = update_argv(command)
        outcome = await transport.run(argv, timeout_s=settings.self_update_timeout_s)
    except (TransportError, ValueError) as exc:
        result.message = f"Could not run the update command: {exc}"
        return result
    result.ran = True
    result.exit_code = outcome.exit_code
    result.output = (outcome.stdout + outcome.stderr).strip()[-2000:]
    result.ok = outcome.ok
    result.message = (
        "Update started - the orchestrator restarts in a moment."
        if outcome.ok
        else f"The update command failed (exit {outcome.exit_code})."
    )
    return result


def schedule_restart(delay_s: float = 1.0) -> None:
    """Stop this process shortly (after the HTTP response is sent). With the
    compose ``restart: unless-stopped`` policy Docker starts it again."""
    loop = asyncio.get_running_loop()
    loop.call_later(delay_s, os.kill, os.getpid(), signal.SIGTERM)
    log.warning("system.restart.scheduled", delay_s=delay_s)
