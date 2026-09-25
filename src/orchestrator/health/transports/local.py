"""Run a command on the orchestrator host itself.

Used for probes that check something reachable from the Pi without entering a
guest, and as the transport for ``ansible_playbook`` probes (ansible runs on the
control host and reaches out itself).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from orchestrator.health.transports.base import (
    CommandResult,
    CommandTransport,
    TransportUnreachable,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class LocalTransport(CommandTransport):
    name = "local"

    def __init__(self, *, cwd: str | None = None) -> None:
        self._cwd = cwd

    async def run(
        self,
        command: Sequence[str],
        *,
        timeout_s: int,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        argv = [str(a) for a in command]
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=env,
            )
        except FileNotFoundError as exc:
            # The executable does not exist. We learned nothing about the
            # service, so this is indeterminate rather than a failing check.
            raise TransportUnreachable(f"local: executable not found: {argv[0]!r} ({exc})") from exc
        except PermissionError as exc:
            raise TransportUnreachable(
                f"local: not permitted to execute {argv[0]!r} ({exc})"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise TransportUnreachable(
                f"local: {argv[0]!r} did not finish within {timeout_s}s"
            ) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        result = CommandResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
            duration_ms=duration_ms,
        )
        log.debug(
            "transport.local.ran",
            argv0=argv[0],
            exit_code=result.exit_code,
            duration_ms=duration_ms,
        )
        return result
