"""The guest gate — the one check that runs before anything else in a scan.

For a service that maps to a Proxmox guest, two facts decide whether probing
it makes any sense at all:

1. is the guest's **node online** in the Proxmox cluster, and
2. is the **guest running**?

If either is false the probes are pointless (they would only time out, and the
resulting failures would describe the wrong problem) and so is the service's
traffic data (every request to it is failing for a reason we already know). The
engine short-circuits to :attr:`HealthState.DOWN` without running a single
probe.

DOWN is deliberately not FAILED: a stopped VM is not evidence of corrupt data,
and it must never look like a restore candidate. It is not UNKNOWN either — we
know exactly what is wrong. The backup gate refuses it like any other
non-HEALTHY verdict (see ``pipelines/backup.py``), and ``Wake before backup`` is
what brings the guest up first.

If Proxmox itself cannot be read, the gate does NOT short-circuit: the probes
run as before and the verdict's reason notes that the guest state is unknown.
An unreachable API must not be allowed to blank out the probes, which may
still have something definite to say.

Fetching is shared: :class:`GuestStateCache` reads ``cluster_status`` and
``list_guests`` at most once each, lazily, and is handed to every scan in one
:class:`~orchestrator.scheduler.health_scanner.HealthScanner` cycle — a cycle
over N services costs two Proxmox calls, not 2N. A one-off ``scan`` gets a
fresh cache of its own, so the backup gate and the wake pipeline's health poll
always see the state as it is right now, never a previous cycle's.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.proxmox.base import ClusterStatus, Guest, ProxmoxAdapter
from orchestrator.domain.enums import GuestKind, HealthState
from orchestrator.domain.schemas import GUEST_CHECK_KIND, ProbeResult

#: Name of the synthetic first entry in a verdict's probe results.
GUEST_CHECK_NAME = "guest"

GuestOutcome = Literal["up", "down", "unknown"]


class GuestStateCache:
    """One read of the cluster's nodes and guests, shared across scans.

    Each fetch happens at most once per cache instance and only when first
    needed — a cycle with no guest-backed services never calls Proxmox. A
    failed fetch is remembered too (as its error string), so a down API costs
    one timeout per cycle rather than one per service.
    """

    def __init__(self, proxmox: ProxmoxAdapter) -> None:
        self._proxmox = proxmox
        self._lock = asyncio.Lock()
        self._cluster: ClusterStatus | None = None
        self._cluster_error: str | None = None
        self._cluster_done = False
        self._guests: list[Guest] | None = None
        self._guests_error: str | None = None
        self._guests_done = False

    async def cluster(self) -> tuple[ClusterStatus | None, str | None]:
        async with self._lock:
            if not self._cluster_done:
                try:
                    self._cluster = await self._proxmox.cluster_status()
                except AdapterError as exc:
                    self._cluster_error = f"{type(exc).__name__}: {exc}"
                self._cluster_done = True
        return self._cluster, self._cluster_error

    async def guests(self) -> tuple[list[Guest] | None, str | None]:
        async with self._lock:
            if not self._guests_done:
                try:
                    self._guests = await self._proxmox.list_guests()
                except AdapterError as exc:
                    self._guests_error = f"{type(exc).__name__}: {exc}"
                self._guests_done = True
        return self._guests, self._guests_error


@dataclass(frozen=True)
class GuestCheck:
    """Outcome of the guest gate for one service."""

    outcome: GuestOutcome
    message: str
    node: str | None = None
    node_online: bool | None = None
    guest_kind: str | None = None
    guest_id: int | None = None
    guest_status: str | None = None

    @property
    def down(self) -> bool:
        return self.outcome == "down"

    def as_probe_result(self) -> ProbeResult:
        state = {
            "up": HealthState.HEALTHY,
            "down": HealthState.DOWN,
            "unknown": HealthState.UNKNOWN,
        }[self.outcome]
        return ProbeResult(
            probe_name=GUEST_CHECK_NAME,
            kind=GUEST_CHECK_KIND,
            state=state,
            message=self.message,
            details={
                "guest_gate": True,
                "outcome": self.outcome,
                "node": self.node,
                "node_online": self.node_online,
                "guest_kind": self.guest_kind,
                "guest_id": self.guest_id,
                "guest_status": self.guest_status,
            },
        )


def has_guest(guest_kind: GuestKind, guest_id: int | None) -> bool:
    return guest_kind is not GuestKind.NONE and guest_id is not None


async def check_guest(
    cache: GuestStateCache,
    *,
    node_name: str | None,
    guest_kind: GuestKind,
    guest_id: int,
) -> GuestCheck:
    """Judge node + guest from ``cache``. Never raises."""
    label = f"{guest_kind.value} {guest_id}"
    base = {"node": node_name, "guest_kind": guest_kind.value, "guest_id": guest_id}

    cluster, cluster_err = await cache.cluster()
    node_online: bool | None = None
    if cluster is None:
        return GuestCheck(
            "unknown",
            f"guest state unknown: Proxmox cluster status unavailable ({cluster_err})",
            **base,
        )
    if node_name is not None:
        entry = next((n for n in cluster.nodes if n.name == node_name), None)
        if entry is not None:
            node_online = entry.online
            if not entry.online:
                return GuestCheck(
                    "down", f"node {node_name!r} is offline", node_online=False, **base
                )

    guests, guests_err = await cache.guests()
    if guests is None:
        return GuestCheck(
            "unknown",
            f"guest state unknown: Proxmox guest list unavailable ({guests_err})",
            node_online=node_online,
            **base,
        )
    guest = next((g for g in guests if g.vmid == guest_id), None)
    if guest is None:
        return GuestCheck(
            "unknown",
            f"guest state unknown: {label} was not found in Proxmox",
            node_online=node_online,
            **base,
        )

    if node_online is None:
        # The registry's node is unset or not in the cluster listing; judge the
        # node Proxmox says the guest actually lives on.
        entry = next((n for n in cluster.nodes if n.name == guest.node), None)
        if entry is not None:
            node_online = entry.online
            if not entry.online:
                return GuestCheck(
                    "down",
                    f"node {guest.node!r} is offline",
                    node_online=False,
                    guest_status=guest.status,
                    **{**base, "node": guest.node},
                )

    status = (guest.status or "").strip().lower()
    where = guest.node or node_name
    detail = {**base, "node": where, "node_online": node_online, "guest_status": guest.status}
    if status == "running":
        return GuestCheck("up", f"node {where!r} online, guest {label} running", **detail)
    if status in {"", "unknown"}:
        return GuestCheck(
            "unknown",
            f"guest state unknown: Proxmox reports {label} as {status or 'blank'}",
            **detail,
        )
    return GuestCheck("down", f"guest {label} is {status}", **detail)


__all__ = [
    "GUEST_CHECK_NAME",
    "GuestCheck",
    "GuestStateCache",
    "check_guest",
    "has_guest",
]
