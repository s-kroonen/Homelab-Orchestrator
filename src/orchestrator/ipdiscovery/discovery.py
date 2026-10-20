"""Find a service's IP address and remember it.

Sources, in order — the first one that answers wins and is recorded:

1. **Proxmox itself** — the QEMU guest agent (VM) or the container's static
   ``ip=`` / ``/interfaces`` (CT), via :meth:`ProxmoxAdapter.guest_network`.
   This also yields the guest's NIC MACs.
2. **ARP** — those MACs looked up in the neighbour table of the host
   ``IP_DISCOVERY_TRANSPORT`` points at (optionally after an "ARP ping"
   sweep of ``IP_DISCOVERY_SWEEP_CIDRS``).
3. **DHCP** — the MACs looked up in a dnsmasq leases file
   (``IP_DISCOVERY_DHCP_LEASES_PATH``) on that same host.

Only guests that Proxmox reports as RUNNING are asked (a stopped VM's agent
cannot answer, and its ARP entry is stale by definition) — the guest state
comes from the same :class:`GuestStateCache` the health engine's guest gate
uses.

What is found lands on :class:`ServiceState` (runtime only). It never touches
``Service.ip_address`` — the operator's manual address — and never reaches
services.yaml, so discovery can't make the registry "dirty". The *effective*
IP is the manual one when set, else the discovered one; a change of the
effective IP is audited (``service.ip_changed``).

Tables are shared per cycle: :class:`DiscoveryCycle` reads the ARP table and
the DHCP leases at most once, lazily, however many services ask.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlmodel import Session

from orchestrator.adapters.proxmox.base import GuestNetwork, ProxmoxAdapter
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import Node, Service, ServiceState
from orchestrator.health.guest_gate import GuestStateCache, check_guest, has_guest
from orchestrator.health.transports.base import (
    CommandTransport,
    TransportError,
    TransportSpec,
)
from orchestrator.ipdiscovery.dhcp import (
    DhcpLeaseSource,
    DnsmasqLeaseFile,
    LeaseReadError,
    leases_by_mac,
)
from orchestrator.ipdiscovery.neighbors import (
    ArpReadError,
    MacTable,
    parse_sweep_cidrs,
    read_arp_table,
    sweep,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Human labels for ``ServiceState.ip_source`` / the effective source.
SOURCE_LABELS = {
    "manual": "manual",
    "guest_agent": "guest agent",
    "lxc": "LXC",
    "arp": "ARP",
    "dhcp": "DHCP",
}

Status = Literal["found", "not_found", "skipped", "error"]


# ---------------------------------------------------------------------------
# effective IP
# ---------------------------------------------------------------------------


def effective_ip(service: Service, state: ServiceState | None) -> tuple[str, str]:
    """``(ip, source)``: the manual address when set, else the discovered one.
    ``("", "")`` when the service has neither."""
    if service.ip_address:
        return service.ip_address, "manual"
    if state is not None and state.discovered_ip:
        return state.discovered_ip, state.ip_source or ""
    return "", ""


def ip_view(service: Service, state: ServiceState | None) -> dict[str, Any]:
    """Everything the UI / API / CLI show about a service's address."""
    ip, source = effective_ip(service, state)
    return {
        "effective_ip": ip,
        "effective_source": source,
        "effective_source_label": SOURCE_LABELS.get(source, source),
        "manual_ip": service.ip_address or "",
        "discovered_ip": state.discovered_ip if state else "",
        "ip_source": state.ip_source if state else "",
        "ip_source_label": SOURCE_LABELS.get(state.ip_source, state.ip_source) if state else "",
        "mac_addresses": list(state.mac_addresses or []) if state else [],
        "ip_discovered_at": (
            state.ip_discovered_at.isoformat() if state and state.ip_discovered_at else None
        ),
        "ip_discovery_error": state.ip_discovery_error if state else "",
        "has_guest": has_guest(service.guest_kind, service.guest_id),
    }


def resolve_service_ip(service_id: int) -> str:
    """The effective IP of a service by id, read in a fresh session — what a
    ping probe with an empty ``host`` targets. "" when it has none."""
    from orchestrator.db.session import session_scope

    with session_scope() as session:
        svc = session.get(Service, service_id)
        if svc is None:
            return ""
        return effective_ip(svc, session.get(ServiceState, service_id))[0]


# ---------------------------------------------------------------------------
# transport setting
# ---------------------------------------------------------------------------


def parse_transport_setting(raw: str) -> TransportSpec | None:
    """``IP_DISCOVERY_TRANSPORT`` -> a TransportSpec, or None when unset.

    JSON object in the shape of a probe's ``transport:`` block, or a bare
    type word (``local``) as shorthand. Raises ValueError on bad JSON.
    """
    from orchestrator.health.engine import _spec_from_config

    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            block = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"IP_DISCOVERY_TRANSPORT is not valid JSON: {exc}") from exc
        if not isinstance(block, dict):
            raise ValueError("IP_DISCOVERY_TRANSPORT must be a JSON object")
    else:
        block = {"type": text}
    return _spec_from_config({"transport": block})


def transport_block(raw: str) -> dict[str, Any] | None:
    """The same setting as a probe ``transport:`` block (what the one-click
    ping probe copies), or None when unset / unparsable."""
    text = (raw or "").strip()
    if not text:
        return None
    if not text.startswith("{"):
        return {"type": text}
    try:
        block = json.loads(text)
    except ValueError:
        return None
    return block if isinstance(block, dict) else None


# ---------------------------------------------------------------------------
# per-cycle shared state
# ---------------------------------------------------------------------------


class DiscoveryCycle:
    """One discovery pass's shared reads: guest states (one Proxmox listing),
    the ARP table and the DHCP leases (one read each, lazily)."""

    def __init__(
        self,
        *,
        guests: GuestStateCache,
        transport: CommandTransport | None,
        sweep_cidrs: str = "",
        arp_enabled: bool = True,
        lease_source: DhcpLeaseSource | None = None,
        transport_error: str | None = None,
    ) -> None:
        self.guests = guests
        self._transport = transport
        self._sweep_cidrs = sweep_cidrs
        self._arp_enabled = arp_enabled
        self._lease_source = lease_source
        self._transport_error = transport_error
        self._lock = asyncio.Lock()
        self._arp: MacTable | None = None
        self._arp_error: str | None = None
        self._arp_done = False
        self._dhcp: MacTable | None = None
        self._dhcp_error: str | None = None
        self._dhcp_done = False
        #: How many times each table was actually read — the "shared per
        #: cycle" guarantee, observable.
        self.reads = {"arp": 0, "dhcp": 0}

    async def arp(self) -> tuple[MacTable | None, str | None]:
        """``(table, problem)``. ``(None, reason)`` when ARP is off/unusable."""
        async with self._lock:
            if not self._arp_done:
                self._arp_done = True
                if self._transport is None:
                    self._arp_error = self._transport_error or (
                        "ARP / DHCP lookup off (IP_DISCOVERY_TRANSPORT not set)"
                    )
                elif not self._arp_enabled:
                    self._arp_error = "ARP lookup off (IP_DISCOVERY_ARP_ENABLED=false)"
                else:
                    self.reads["arp"] += 1
                    try:
                        networks = parse_sweep_cidrs(self._sweep_cidrs)
                    except ValueError as exc:
                        networks = []
                        log.warning("ipdiscovery.sweep.bad_cidrs", error=str(exc))
                    if networks:
                        for problem in await sweep(self._transport, networks):
                            log.warning("ipdiscovery.sweep.problem", problem=problem)
                    try:
                        self._arp = await read_arp_table(self._transport)
                    except ArpReadError as exc:
                        self._arp_error = str(exc)
        return self._arp, self._arp_error

    async def dhcp(self) -> tuple[MacTable | None, str | None]:
        async with self._lock:
            if not self._dhcp_done:
                self._dhcp_done = True
                if self._lease_source is None and self._transport is not None:
                    self._dhcp_error = "DHCP lookup off (IP_DISCOVERY_DHCP_LEASES_PATH not set)"
                elif self._lease_source is None:
                    # No transport at all: the ARP miss already said so.
                    self._dhcp_error = None
                else:
                    self.reads["dhcp"] += 1
                    try:
                        self._dhcp = leases_by_mac(await self._lease_source.leases())
                    except LeaseReadError as exc:
                        self._dhcp_error = str(exc)
        return self._dhcp, self._dhcp_error


@dataclass
class DiscoveryResult:
    slug: str
    status: Status
    message: str
    ip: str = ""
    source: str = ""
    macs: list[str] = field(default_factory=list)
    effective_ip: str = ""
    effective_source: str = ""
    changed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "status": self.status,
            "message": self.message,
            "ip": self.ip,
            "source": self.source,
            "macs": list(self.macs),
            "effective_ip": self.effective_ip,
            "effective_source": self.effective_source,
            "changed": self.changed,
        }


def _lookup(table: MacTable | None, macs: list[str]) -> str:
    for mac in macs:
        ips = (table or {}).get(mac)
        if ips:
            return ips[0]
    return ""


# ---------------------------------------------------------------------------
# the discoverer
# ---------------------------------------------------------------------------


class IpDiscoverer:
    """Discovers and stores service IPs. One per scheduler / request."""

    def __init__(
        self,
        *,
        settings: Settings,
        proxmox: ProxmoxAdapter,
        transport: CommandTransport | None = None,
        lease_source: DhcpLeaseSource | None = None,
    ) -> None:
        """``transport`` / ``lease_source`` override what the settings would
        build (tests). Unset, ``IP_DISCOVERY_TRANSPORT`` decides."""
        self._settings = settings
        self._proxmox = proxmox
        self._transport_override = transport
        self._lease_override = lease_source

    def _build_transport(self) -> tuple[CommandTransport | None, str | None]:
        if self._transport_override is not None:
            return self._transport_override, None
        try:
            spec = parse_transport_setting(self._settings.ip_discovery_transport)
        except ValueError as exc:
            return None, str(exc)
        if spec is None:
            return None, None
        from orchestrator.health.engine import TransportFactory

        try:
            return TransportFactory(self._settings).build(spec), None
        except TransportError as exc:
            return None, f"IP_DISCOVERY_TRANSPORT is unusable: {exc}"

    def new_cycle(self) -> DiscoveryCycle:
        transport, transport_error = self._build_transport()
        lease_source = self._lease_override
        path = self._settings.ip_discovery_dhcp_leases_path.strip()
        if lease_source is None and transport is not None and path:
            lease_source = DnsmasqLeaseFile(transport, path)
        return DiscoveryCycle(
            guests=GuestStateCache(self._proxmox),
            transport=transport,
            sweep_cidrs=self._settings.ip_discovery_sweep_cidrs,
            arp_enabled=self._settings.ip_discovery_arp_enabled,
            lease_source=lease_source,
            transport_error=transport_error,
        )

    async def discover(
        self,
        session: Session,
        service: Service,
        *,
        cycle: DiscoveryCycle | None = None,
        actor: str = "system",
    ) -> DiscoveryResult:
        """Discover ``service``'s IP, store it, audit a change. Never raises
        for an infrastructure problem — that is a ``status="error"`` result."""
        cycle = cycle or self.new_cycle()
        slug = service.slug
        if not has_guest(service.guest_kind, service.guest_id):
            return self._finish(
                session,
                service,
                DiscoveryResult(
                    slug,
                    "skipped",
                    "no Proxmox guest to ask — set the IP manually",
                ),
                actor=actor,
                record=False,
            )
        assert service.guest_id is not None
        node = session.get(Node, service.node_id) if service.node_id is not None else None
        check = await check_guest(
            cycle.guests,
            node_name=node.name if node else None,
            guest_kind=service.guest_kind,
            guest_id=service.guest_id,
        )
        if check.outcome != "up":
            # Down (stopped / node off) or unknown: either way we cannot say
            # it is running, and a stopped VM's agent cannot answer.
            return self._finish(
                session,
                service,
                DiscoveryResult(slug, "skipped", f"not asked: {check.message}"),
                actor=actor,
                record=False,
            )

        where = check.node or (node.name if node else "")
        try:
            net = await self._proxmox.guest_network(where, service.guest_id, service.guest_kind)
        except Exception as exc:  # an adapter bug must not stop the cycle
            log.warning("ipdiscovery.guest_network.failed", service=slug, error=str(exc))
            net = GuestNetwork(error=f"{type(exc).__name__}: {exc}")

        if net.ips:
            return self._finish(
                session,
                service,
                DiscoveryResult(
                    slug,
                    "found",
                    f"{net.ips[0]} from the {_label(net.source)}",
                    ip=net.ips[0],
                    source=net.source or "",
                    macs=net.macs,
                ),
                actor=actor,
            )

        misses: list[str] = []
        if net.error:
            misses.append(net.error)
        else:
            misses.append(
                "the guest agent gave no address"
                if service.guest_kind.value == "vm"
                else "the container reported no address"
            )
        if not net.macs:
            misses.append("no NIC MAC to look up")
            return self._finish(
                session,
                service,
                DiscoveryResult(slug, "error" if net.error else "not_found", "; ".join(misses)),
                actor=actor,
            )

        for source, read in (("arp", cycle.arp), ("dhcp", cycle.dhcp)):
            table, problem = await read()
            ip = _lookup(table, net.macs)
            if ip:
                return self._finish(
                    session,
                    service,
                    DiscoveryResult(
                        slug,
                        "found",
                        f"{ip} from {_label(source)} ({', '.join(net.macs)})",
                        ip=ip,
                        source=source,
                        macs=net.macs,
                    ),
                    actor=actor,
                )
            if problem:
                misses.append(problem)
            elif table is not None:
                misses.append(f"MAC not in the {_label(source)}")

        return self._finish(
            session,
            service,
            DiscoveryResult(slug, "not_found", "; ".join(misses), macs=net.macs),
            actor=actor,
        )

    def _finish(
        self,
        session: Session,
        service: Service,
        result: DiscoveryResult,
        *,
        actor: str,
        record: bool = True,
    ) -> DiscoveryResult:
        """Persist ``result`` on ServiceState and audit an effective-IP change.
        ``record=False`` (skipped) leaves the stored discovery untouched."""
        assert service.id is not None
        state = session.get(ServiceState, service.id)
        before = effective_ip(service, state)
        if record:
            if state is None:
                state = ServiceState(service_id=service.id)
                session.add(state)
            if result.macs:
                state.mac_addresses = list(result.macs)
            if result.status == "found":
                state.discovered_ip = result.ip
                state.ip_source = result.source
                state.ip_discovered_at = datetime.now(UTC)
                state.ip_discovery_error = ""
            else:
                # Keep the last known address: an agent that stopped answering
                # does not mean the address changed.
                state.ip_discovery_error = result.message[:500]
            session.add(state)
        after = effective_ip(service, state)
        result.effective_ip, result.effective_source = after
        if after[0] != before[0]:
            result.changed = True
            audit.record(
                session,
                actor=actor,
                action="service.ip_changed",
                target=f"service:{service.slug}",
                details={
                    "old": before[0],
                    "new": after[0],
                    "source": after[1],
                    "macs": list(result.macs),
                },
            )
        session.commit()
        log.info(
            "ipdiscovery.service",
            service=service.slug,
            status=result.status,
            ip=result.ip,
            source=result.source,
            effective=after[0],
            changed=result.changed,
        )
        return result


def _label(source: str | None) -> str:
    return {
        "guest_agent": "guest agent",
        "lxc": "container config",
        "arp": "ARP table",
        "dhcp": "DHCP leases",
    }.get(source or "", source or "?")


__all__ = [
    "SOURCE_LABELS",
    "DiscoveryCycle",
    "DiscoveryResult",
    "IpDiscoverer",
    "effective_ip",
    "ip_view",
    "parse_transport_setting",
    "resolve_service_ip",
    "transport_block",
]
