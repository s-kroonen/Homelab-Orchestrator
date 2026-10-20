"""DHCP lease lookups: MAC -> IP from the DHCP server's own records.

Pluggable on purpose. :class:`DhcpLeaseSource` is the seam; today there is one
implementation, :class:`DnsmasqLeaseFile`, which ``cat``s a dnsmasq-format
leases file over a command transport (host runner / ssh / local). Readers for
OPNsense / pfSense (their APIs) and Kea (its lease database or control
socket) slot in as further subclasses — see docs/roadmap.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from orchestrator.adapters.proxmox.net import normalize_mac, usable_ip
from orchestrator.health.transports.base import CommandTransport, TransportError
from orchestrator.ipdiscovery.neighbors import MacTable


@dataclass(frozen=True)
class DhcpLease:
    mac: str
    ip: str
    hostname: str = ""
    #: None = infinite lease (dnsmasq writes expiry 0).
    expires_at: datetime | None = None


class LeaseReadError(Exception):
    """The lease source could not be read."""


class DhcpLeaseSource(ABC):
    """Somewhere DHCP leases can be read from."""

    #: Short label for messages ("dnsmasq leases at /var/lib/misc/...").
    label: str = "dhcp"

    @abstractmethod
    async def leases(self) -> list[DhcpLease]:
        """Every current lease. Raises :class:`LeaseReadError` on failure."""


def parse_dnsmasq_leases(text: str, *, now: datetime | None = None) -> list[DhcpLease]:
    """Parse a dnsmasq leases file: ``expiry mac ip hostname clientid`` per line.

    Expired leases are dropped. DHCPv6 lines (``duid ...`` and the IAID-keyed
    lines after it) carry no MAC and drop out on their own.
    """
    now = now or datetime.now(UTC)
    out: list[DhcpLease] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mac = normalize_mac(parts[1])
        ip = usable_ip(parts[2])
        if not mac or not ip:
            continue
        try:
            expiry = int(parts[0])
        except ValueError:
            continue
        expires_at = None if expiry == 0 else datetime.fromtimestamp(expiry, UTC)
        if expires_at is not None and expires_at < now:
            continue
        hostname = parts[3] if len(parts) > 3 and parts[3] != "*" else ""
        out.append(DhcpLease(mac=mac, ip=ip, hostname=hostname, expires_at=expires_at))
    return out


def leases_by_mac(leases: list[DhcpLease]) -> MacTable:
    """``mac -> [ip, ...]``, the longest-lived lease first."""
    far = datetime.max.replace(tzinfo=UTC)
    ordered = sorted(leases, key=lambda lease: lease.expires_at or far, reverse=True)
    table: MacTable = {}
    for lease in ordered:
        ips = table.setdefault(lease.mac, [])
        if lease.ip not in ips:
            ips.append(lease.ip)
    return table


class DnsmasqLeaseFile(DhcpLeaseSource):
    """A dnsmasq leases file read with ``cat`` over a command transport."""

    def __init__(self, transport: CommandTransport, path: str, *, timeout_s: int = 20) -> None:
        self._transport = transport
        self._path = path
        self._timeout_s = timeout_s
        self.label = f"dnsmasq leases at {path}"

    async def leases(self) -> list[DhcpLease]:
        try:
            result = await self._transport.run(["cat", self._path], timeout_s=self._timeout_s)
        except TransportError as exc:
            raise LeaseReadError(f"could not read {self._path}: {exc}") from exc
        if not result.ok:
            raise LeaseReadError(f"cat {self._path} exited {result.exit_code}: {result.tail(200)}")
        return parse_dnsmasq_leases(result.stdout)


__all__ = [
    "DhcpLease",
    "DhcpLeaseSource",
    "DnsmasqLeaseFile",
    "LeaseReadError",
    "leases_by_mac",
    "parse_dnsmasq_leases",
]
