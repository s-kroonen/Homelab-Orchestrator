"""SSH transport, via asyncssh.

Auth is key-based only. Passwords are deliberately unsupported: this runs
unattended, so a password would have to live in config, and the Pi already has
keys as the Ansible control host.

Host-key policy defaults to *verifying* against a known_hosts file. That is a
real security boundary — the probes carry DB credentials in some configs, and
accepting any host key would let anything on the path harvest them. Verification
can be disabled explicitly for a lab, and doing so logs a warning.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path

from orchestrator.health.transports.base import (
    CommandResult,
    CommandTransport,
    TransportConfigError,
    TransportUnreachable,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class SshTransport(CommandTransport):
    """One transport instance per (host, user). Connections are opened per run;
    asyncssh keeps this cheap enough for probe cadence, and a fresh connection
    avoids a stale pooled socket reporting a false UNKNOWN."""

    name = "ssh"

    def __init__(
        self,
        *,
        host: str,
        user: str = "root",
        port: int = 22,
        key_path: str | None = None,
        known_hosts_path: str | None = None,
        verify_host_key: bool = True,
        connect_timeout_s: int = 10,
        jump_host: str | None = None,
        jump_user: str | None = None,
        jump_port: int = 22,
    ) -> None:
        if not host:
            raise TransportConfigError("ssh transport requires a host")
        self._host = host
        self._user = user
        self._port = port
        self._key_path = key_path
        self._known_hosts_path = known_hosts_path
        self._verify_host_key = verify_host_key
        self._connect_timeout_s = connect_timeout_s
        # ProxyJump. Needed wherever the orchestrator is not on the guests'
        # network and can only reach them through a bastion/gateway.
        self._jump_host = jump_host
        self._jump_user = jump_user or user
        self._jump_port = jump_port

        if not verify_host_key:
            log.warning(
                "transport.ssh.host_key_verification_disabled",
                host=host,
                impact="A machine-in-the-middle could capture anything these "
                "probes send, including DB credentials in probe configs.",
            )

    async def run(
        self,
        command: Sequence[str],
        *,
        timeout_s: int,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        try:
            import asyncssh
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise TransportUnreachable(
                "ssh transport needs the 'asyncssh' package, which is not installed"
            ) from exc

        argv = [str(a) for a in command]
        connect_kwargs: dict[str, object] = {
            "host": self._host,
            "port": self._port,
            "username": self._user,
            "connect_timeout": self._connect_timeout_s,
        }

        if self._key_path:
            key = Path(self._key_path)
            if not key.exists():
                raise TransportConfigError(
                    f"ssh key not found at {self._key_path!r}. In Docker this is "
                    "usually a missing bind mount — the key must be mounted into "
                    "the container, not just present on the host."
                )
            connect_kwargs["client_keys"] = [str(key)]

        if not self._verify_host_key:
            connect_kwargs["known_hosts"] = None
        elif self._known_hosts_path:
            kh = Path(self._known_hosts_path)
            if not kh.exists():
                raise TransportConfigError(
                    f"known_hosts not found at {self._known_hosts_path!r}. Either "
                    "mount it, or set verify_host_key=false to accept any key "
                    "(understanding the exposure)."
                )
            connect_kwargs["known_hosts"] = str(kh)

        started = time.monotonic()
        try:
            async with AsyncExitStack() as stack:
                if self._jump_host:
                    # Open the jump connection separately rather than passing
                    # tunnel="user@host". It costs a few lines and buys the
                    # distinction that matters most here: a failure reaching the
                    # GATEWAY reads differently from a failure reaching the
                    # service behind it.
                    jump_kwargs = dict(connect_kwargs)
                    jump_kwargs.update(
                        host=self._jump_host,
                        port=self._jump_port,
                        username=self._jump_user,
                    )
                    try:
                        tunnel = await stack.enter_async_context(
                            asyncssh.connect(**jump_kwargs)  # type: ignore[arg-type]
                        )
                    except (OSError, asyncssh.Error, TimeoutError) as exc:
                        raise TransportUnreachable(
                            f"ssh: could not reach the JUMP HOST "
                            f"{self._jump_user}@{self._jump_host}:{self._jump_port} ({exc}). "
                            f"The target {self._host} was never contacted, so this says "
                            f"nothing about it."
                        ) from exc
                    connect_kwargs["tunnel"] = tunnel

                conn = await stack.enter_async_context(
                    asyncssh.connect(**connect_kwargs)  # type: ignore[arg-type]
                )
                completed = await conn.run(
                    argv,
                    check=False,  # non-zero is a RESULT, not an exception
                    timeout=timeout_s,
                    env=env or {},
                )
        except TransportUnreachable:
            raise  # already carries the better jump-host message
        except asyncssh.HostKeyNotVerifiable as exc:
            raise TransportUnreachable(
                f"ssh: host key for {self._host} is not in known_hosts ({exc}). "
                "Add it, or set verify_host_key=false for this probe."
            ) from exc
        except asyncssh.PermissionDenied as exc:
            # Auth failed. We learned nothing about the service.
            raise TransportUnreachable(
                f"ssh: authentication rejected by {self._user}@{self._host} ({exc})"
            ) from exc
        except (OSError, asyncssh.Error) as exc:
            via = f" via jump host {self._jump_host}" if self._jump_host else ""
            raise TransportUnreachable(
                f"ssh: could not run command on "
                f"{self._user}@{self._host}:{self._port}{via} ({exc})"
            ) from exc
        except TimeoutError as exc:
            raise TransportUnreachable(
                f"ssh: command on {self._host} did not finish within {timeout_s}s"
            ) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResult(
            exit_code=completed.exit_status if completed.exit_status is not None else -1,
            stdout=_as_text(completed.stdout),
            stderr=_as_text(completed.stderr),
            duration_ms=duration_ms,
        )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
