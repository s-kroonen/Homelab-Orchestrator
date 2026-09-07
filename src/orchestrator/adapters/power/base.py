"""Interface to the existing power manager.

The power manager is a separate service that already exists — it accepts
commands over MQTT / HTTP / IPMI and reports node power state on MQTT topics.
This orchestrator does NOT reimplement it; we speak to it.

Every long-running pipeline (wake, backup) asserts a HOLD to inhibit the
manager's usage-based shutdown while work is in flight, then RELEASES the
hold when done.  Holds are refcounted server-side by the manager — we still
track our own count in ``NodeState.hold_count`` for diagnostics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from orchestrator.domain.enums import PowerState


@dataclass(frozen=True)
class NodeReport:
    """One-shot status observed from the power manager."""

    node_name: str
    state: PowerState
    detail: str | None = None


class PowerAdapter(ABC):
    """All methods are async so callers stay non-blocking; sync adapters can
    trivially wrap their sync client."""

    @abstractmethod
    async def start(self) -> None:
        """Open transport (MQTT connection, HTTP client, …). Idempotent."""

    @abstractmethod
    async def stop(self) -> None:
        """Close transport. Idempotent."""

    @abstractmethod
    async def wake(self, node_name: str, *, reason: str) -> None:
        """Fire-and-forget wake request. State comes back via :meth:`get_status`
        or (later) an event stream."""

    @abstractmethod
    async def power_off(self, node_name: str, *, reason: str) -> None:
        """Request a graceful power-off. Only used by the greenlight flow."""

    @abstractmethod
    async def hold(self, node_name: str, *, reason: str, ttl_s: int) -> str:
        """Assert an inhibit-shutdown hold; return an opaque handle to release with.
        ``ttl_s`` bounds the hold so a crashed orchestrator can't pin a node
        forever."""

    @abstractmethod
    async def release(self, node_name: str, *, handle: str) -> None:
        """Release a previously-asserted hold."""

    @abstractmethod
    async def get_status(self, node_name: str) -> NodeReport:
        """Query the last-known power state for one node."""
