"""Recording transport for tests and DRY_RUN.

Records what would have been run and returns a scripted result. Tests drive the
three-state model through it by queueing results or raising
:class:`TransportUnreachable`.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestrator.health.transports.base import (
    CommandResult,
    CommandTransport,
    TransportUnreachable,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class DryRunTransport(CommandTransport):
    name = "dry_run"

    def __init__(
        self,
        *,
        default_result: CommandResult | None = None,
        unreachable: bool = False,
    ) -> None:
        # In real DRY_RUN we return success: the point is to exercise the
        # pipeline's shape, and a fabricated failure would make the whole run
        # look broken for no reason. Tests override per-command.
        self.default_result = default_result or CommandResult(exit_code=0, stdout="dry-run")
        self.unreachable = unreachable
        self.calls: list[list[str]] = []
        self.scripted: dict[str, CommandResult] = {}

    def script(self, argv0_contains: str, result: CommandResult) -> None:
        """Make any command whose joined argv contains ``argv0_contains`` return
        ``result``. Substring matching keeps tests readable."""
        self.scripted[argv0_contains] = result

    async def run(
        self,
        command: Sequence[str],
        *,
        timeout_s: int,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        argv = [str(a) for a in command]
        self.calls.append(argv)
        joined = " ".join(argv)
        log.info("transport.dry_run.run", command=joined, timeout_s=timeout_s)

        if self.unreachable:
            raise TransportUnreachable(f"dry_run: pretending {joined!r} is unreachable")

        for needle, result in self.scripted.items():
            if needle in joined:
                return result
        return self.default_result
