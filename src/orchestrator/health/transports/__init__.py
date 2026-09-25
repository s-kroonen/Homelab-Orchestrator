"""Command transports — how a probe reaches what it checks."""

from orchestrator.health.transports.base import (
    CommandResult,
    CommandTransport,
    TransportConfigError,
    TransportError,
    TransportSpec,
    TransportUnreachable,
)

__all__ = [
    "CommandResult",
    "CommandTransport",
    "TransportConfigError",
    "TransportError",
    "TransportSpec",
    "TransportUnreachable",
]
