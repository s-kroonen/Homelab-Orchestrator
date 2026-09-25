"""Command transports — *how* a probe reaches the thing it checks.

Deliberately separate from *what* is checked (``ProbeKind``). A systemd check is
the same check whether it runs over SSH or locally; only the transport differs.

THE CRITICAL DISTINCTION, and the reason this layer exists at all:

    command ran, exit code != 0   ->  returned in CommandResult  ->  FAILED
    could not run the command     ->  raises TransportUnreachable ->  UNKNOWN

That mapping is the whole three-state health model (spec section 4). A refused
SSH connection must never look like a failing service, because FAILED marks a
service as a restore candidate while UNKNOWN only alerts. Conflating them is how
a network blip turns into a restore proposal.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a command that *actually ran*. A non-zero exit is a result,
    not an error — it means the check ran and the answer was no."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def tail(self, limit: int = 400) -> str:
        """Best available explanation, preferring stderr. For probe messages."""
        text = (self.stderr or self.stdout).strip()
        return text[-limit:] if text else ""


class TransportError(Exception):
    """Base for transport-level problems."""


class TransportUnreachable(TransportError):
    """The command could not be run at all — connect refused, auth rejected,
    host unknown, timed out before producing an exit code, transport binary
    missing.

    Indeterminate by definition. Callers MUST map this to
    ``HealthState.UNKNOWN``, never to FAILED.
    """


class TransportConfigError(TransportError):
    """The transport is misconfigured (missing host, unreadable key path).

    Also indeterminate — we learned nothing about the service — but worth its
    own type because the fix is in our config rather than out on the network.
    """


@dataclass(frozen=True)
class TransportSpec:
    """Declarative transport config, as it appears in a probe's YAML.

    ``type`` selects the implementation; the rest are per-type and validated by
    the implementation that consumes them.
    """

    type: str = "local"
    host: str | None = None
    user: str | None = None
    port: int | None = None
    extra: dict[str, object] = field(default_factory=dict)


class CommandTransport(ABC):
    """Runs a command somewhere and reports how it went."""

    name: str = "abstract"

    @abstractmethod
    async def run(
        self,
        command: Sequence[str],
        *,
        timeout_s: int,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Execute ``command`` (argv form, never a shell string).

        argv rather than a shell string on purpose: probe configs come from YAML
        that an operator edits, and a shell string there is a command-injection
        surface for anyone who can write the registry. Callers that genuinely
        need shell semantics pass an explicit ``["sh", "-lc", "..."]``.

        Raises :class:`TransportUnreachable` if the command could not be run.
        Returns a :class:`CommandResult` — including for non-zero exits.
        """

    async def close(self) -> None:
        """Release any pooled connections. Idempotent; default is a no-op."""
        return None
