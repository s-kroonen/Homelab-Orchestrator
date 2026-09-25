"""Talk to the host runner over a unix socket.

The container holds no inventory, no playbooks and no SSH key. It asks the
runner — which lives on the host, outside this container — to perform a check
the runner's own config already permits, and gets back a result.

    container  ──unix socket──>  runner (host)  ──ansible/ssh──>  guests
      no keys                     owns keys

Two things follow from that split, and both matter more than the plumbing:

* **A compromised container cannot read the credentials**, because they were
  never here. It can only ask for checks the operator pre-approved.
* **A refusal is not a service verdict.** If the runner declines a request
  (a playbook not on its allowlist, a host outside its scope) that is a
  *configuration* problem on our side, not evidence about the service — so it
  maps to UNKNOWN, exactly like an unreachable host.

See ``contrib/host-runner/`` for the runner and ``docs/host_runner.md`` for the
setup.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Any

from orchestrator.health.transports.base import (
    CommandResult,
    CommandTransport,
    TransportConfigError,
    TransportUnreachable,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


async def _open_connection(socket_path: str):
    """Indirection so tests can substitute a fake without needing AF_UNIX.

    Also gives a clear error on Windows, where the orchestrator is only ever
    run for development.
    """
    opener = getattr(asyncio, "open_unix_connection", None)
    if opener is None:  # pragma: no cover - platform dependent
        raise TransportConfigError(
            "unix sockets are not available on this platform; the host runner "
            "transport is for the Linux host the orchestrator deploys to"
        )
    return await opener(socket_path)


class HostAgentTransport(CommandTransport):
    """Runs a check by asking the host runner to do it."""

    name = "host_agent"

    def __init__(
        self,
        *,
        socket_path: str,
        host: str | None = None,
        action: str = "command",
        playbook: str | None = None,
        extra_vars: dict[str, Any] | None = None,
        connect_timeout_s: int = 10,
    ) -> None:
        if not socket_path:
            raise TransportConfigError(
                "host_agent transport requires a socket path — set "
                "HOST_RUNNER_SOCKET or `socket_path` in the probe's transport block"
            )
        self._socket_path = socket_path
        self._host = host
        self._action = action
        self._playbook = playbook
        self._extra_vars = extra_vars or {}
        self._connect_timeout_s = connect_timeout_s

    async def run(
        self,
        command: Sequence[str],
        *,
        timeout_s: int,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        request = self._build_request(command, timeout_s)
        started = time.monotonic()
        payload = await self._exchange(request, timeout_s)
        duration_ms = int((time.monotonic() - started) * 1000)

        if payload.get("unreachable"):
            raise TransportUnreachable(
                f"host runner: {payload.get('error', 'target unreachable')}. "
                f"The check never ran, so this says nothing about the service."
            )

        if payload.get("refused"):
            # The runner declined. That is our misconfiguration, not a verdict.
            raise TransportConfigError(
                f"the host runner refused this request: {payload.get('error')}. "
                f"Add it to the runner's allowlist if it is genuinely wanted."
            )

        if "error" in payload and "rc" not in payload:
            raise TransportUnreachable(f"host runner error: {payload['error']}")

        return CommandResult(
            exit_code=int(payload.get("rc", 1)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            duration_ms=duration_ms,
        )

    async def hello(self) -> dict[str, Any]:
        """Ask the runner what it is willing to do. Used by `transport check`."""
        return await self._exchange({"action": "hello"}, timeout_s=10)

    # -- internals ----------------------------------------------------------

    def _build_request(self, command: Sequence[str], timeout_s: int) -> dict[str, Any]:
        request: dict[str, Any] = {"action": self._action, "timeout_s": timeout_s}
        if self._host:
            request["host"] = self._host
        if self._action == "playbook":
            if not self._playbook:
                raise TransportConfigError(
                    "the playbook action needs a `playbook` name (the runner "
                    "resolves names against its own playbook_dir — paths are "
                    "deliberately not accepted)"
                )
            request["playbook"] = self._playbook
            if self._host:
                request["limit"] = self._host
            if self._extra_vars:
                request["extra_vars"] = self._extra_vars
        elif self._action == "command":
            request["argv"] = [str(a) for a in command]
        return request

    async def _exchange(self, request: dict[str, Any], timeout_s: int) -> dict[str, Any]:
        try:
            reader, writer = await asyncio.wait_for(
                _open_connection(self._socket_path), timeout=self._connect_timeout_s
            )
        except TransportConfigError:
            raise
        except TimeoutError as exc:
            raise TransportUnreachable(
                f"host runner did not accept a connection on {self._socket_path} "
                f"within {self._connect_timeout_s}s"
            ) from exc
        except FileNotFoundError as exc:
            raise TransportUnreachable(
                f"no host runner socket at {self._socket_path}. Is "
                f"orchestrator-host-runner running on the host, and is the socket "
                f"mounted into this container? ({exc})"
            ) from exc
        except PermissionError as exc:
            raise TransportUnreachable(
                f"permission denied on {self._socket_path}. The container's user "
                f"needs to be in the socket's group — see docs/host_runner.md. ({exc})"
            ) from exc
        except OSError as exc:
            raise TransportUnreachable(
                f"could not reach the host runner at {self._socket_path}: {exc}"
            ) from exc

        try:
            writer.write((json.dumps(request) + "\n").encode("utf-8"))
            await writer.drain()
            # Generous: the runner is holding the connection open for the whole
            # check, which for a DB integrity playbook is minutes.
            raw = await asyncio.wait_for(
                reader.readline(), timeout=timeout_s + self._connect_timeout_s
            )
        except TimeoutError as exc:
            raise TransportUnreachable(f"host runner did not answer within {timeout_s}s") from exc
        except OSError as exc:
            raise TransportUnreachable(f"host runner connection failed: {exc}") from exc
        finally:
            writer.close()

        if not raw:
            raise TransportUnreachable("host runner closed the connection without replying")
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise TransportUnreachable("host runner reply was implausibly large")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise TransportUnreachable(f"host runner sent a malformed reply: {exc}") from exc
        if not isinstance(payload, dict):
            raise TransportUnreachable("host runner reply was not an object")
        return payload
