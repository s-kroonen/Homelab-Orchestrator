"""Interface to the existing power manager.

The power manager is a separate service that already exists — it accepts
commands over MQTT / HTTP / IPMI and reports node power state on MQTT topics.
This orchestrator does NOT reimplement it; we speak to it.

Every long-running pipeline (wake, backup) asserts a HOLD to inhibit the
manager's usage-based shutdown while work is in flight, then RELEASES the
hold when done.  Holds are refcounted server-side by the manager — we still
track our own count in ``NodeState.hold_count`` for diagnostics.

**Not every manager implements every seam.** The real device in this deployment
(a Home Assistant MQTT integration over IPMI/iLO) exposes power actions but no
inhibit-shutdown concept at all — see :class:`~orchestrator.adapters.power.mqtt.
MqttPowerAdapter`, which implements ``hold``/``release`` as local no-ops rather
than inventing a protocol the device does not have.
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
    trivially wrap their sync client.

    ``node_name`` in every method below is ``Node.power_mgr_target`` — the
    opaque identifier the power manager itself uses (an MQTT topic segment,
    for the real device), not necessarily the Proxmox node name.
    """

    # Set False on adapters that are still a stub for a later phase. The factory
    # checks this and substitutes the dry-run implementation rather than handing
    # back an object whose start() raises and crash-loops the container.
    IMPLEMENTED: bool = True

    @abstractmethod
    async def start(self) -> None:
        """Open transport (MQTT connection, HTTP client, …). Idempotent."""

    @abstractmethod
    async def stop(self) -> None:
        """Close transport. Idempotent."""

    @abstractmethod
    async def wake(self, node_name: str, *, reason: str) -> None:
        """Fire-and-forget power-on request. State comes back via
        :meth:`get_status` or (later) an event stream."""

    @abstractmethod
    async def power_off(self, node_name: str, *, reason: str, force: bool = False) -> None:
        """Request the node power off. Only used by the greenlight flow and
        manual operator action — the wake pipeline never powers anything off.

        ``force=False`` (default) asks for a graceful shutdown; the node's own
        OS/BMC decides how, and may not act immediately or at all if the OS is
        unresponsive. ``force=True`` cuts power immediately, with no chance for
        a clean shutdown — data loss on whatever was running.
        """

    @abstractmethod
    async def restart(self, node_name: str, *, reason: str, force: bool = False) -> None:
        """Request the node restart.

        ``force=False`` (default) sends the manager's normal reset action.
        ``force=True`` is a full power-cycle (off, then on) — a stronger action
        for a node a plain reset did not recover, and more disruptive.
        """

    @abstractmethod
    async def hold(self, node_name: str, *, reason: str, ttl_s: int) -> str:
        """Assert an inhibit-shutdown hold; return an opaque handle to release with.
        ``ttl_s`` bounds the hold so a crashed orchestrator can't pin a node
        forever.

        Not every manager can actually inhibit anything server-side — an
        adapter without that capability may implement this as a local no-op.
        Callers still hold their own accounting (``NodeState.hold_count``) and
        must not treat this call's success as a guarantee the node cannot be
        powered off by something else.
        """

    @abstractmethod
    async def release(self, node_name: str, *, handle: str) -> None:
        """Release a previously-asserted hold."""

    @abstractmethod
    async def get_status(self, node_name: str) -> NodeReport:
        """Query the last-known power state for one node."""
