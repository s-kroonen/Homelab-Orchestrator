"""ARP (neighbour table) reads and the optional "ARP ping" sweep.

Everything runs over a :class:`~orchestrator.health.transports.base.CommandTransport`
— the same host-runner / ssh / local plumbing probes use — so the table read is
the one on the host that actually shares a LAN with the guests (usually the
gateway), not the container's own Docker bridge.

Two table formats are understood::

    ip neigh show   192.168.1.10 dev eth0 lladdr bc:24:11:aa:bb:cc REACHABLE
    arp -an         ? (192.168.1.10) at bc:24:11:aa:bb:cc [ether] on eth0

FAILED / INCOMPLETE entries (no answer to the ARP request) are ignored.
"""

from __future__ import annotations

import ipaddress
import re

from orchestrator.adapters.proxmox.net import normalize_mac, usable_ip
from orchestrator.health.transports.base import CommandTransport, TransportError
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: mac -> addresses seen for it, IPv4 first.
MacTable = dict[str, list[str]]

_BAD_STATES = {"FAILED", "INCOMPLETE"}
_ARP_AN = re.compile(r"\((?P<ip>[^)]+)\)\s+at\s+(?P<mac>[0-9A-Fa-f:.-]+)")
#: Largest sweep per CIDR (a /22). Anything bigger is refused — a homelab LAN
#: is a /24, and pinging a /16 from a gateway is a good way to get noticed.
MAX_SWEEP_HOSTS = 1024
#: Parallel pings per batch in the fallback loop.
SWEEP_BATCH = 64
_COMMAND_NOT_FOUND = 127


def _add(table: MacTable, mac: str, ip: str) -> None:
    ips = table.setdefault(mac, [])
    if ip not in ips:
        ips.append(ip)


def _v4_first(table: MacTable) -> MacTable:
    return {
        mac: [i for i in ips if ":" not in i] + [i for i in ips if ":" in i]
        for mac, ips in table.items()
    }


def parse_ip_neigh(text: str) -> MacTable:
    """Parse ``ip neigh show`` output into ``mac -> [ip, ...]``."""
    table: MacTable = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or "lladdr" not in parts:
            continue
        if parts[-1].upper() in _BAD_STATES:
            continue
        ip = usable_ip(parts[0])
        idx = parts.index("lladdr")
        mac = normalize_mac(parts[idx + 1]) if idx + 1 < len(parts) else None
        if ip and mac:
            _add(table, mac, ip)
    return _v4_first(table)


def parse_arp_an(text: str) -> MacTable:
    """Parse ``arp -an`` output into ``mac -> [ip, ...]``. ``<incomplete>``
    entries do not match the MAC pattern and drop out."""
    table: MacTable = {}
    for line in text.splitlines():
        m = _ARP_AN.search(line)
        if not m:
            continue
        ip = usable_ip(m.group("ip"))
        mac = normalize_mac(m.group("mac"))
        if ip and mac:
            _add(table, mac, ip)
    return _v4_first(table)


async def read_arp_table(transport: CommandTransport, *, timeout_s: int = 20) -> MacTable:
    """Read the neighbour table: ``ip neigh show``, else ``arp -an``.

    Raises :class:`ArpReadError` when neither could be run.
    """
    problems: list[str] = []
    for argv, parse in ((["ip", "neigh", "show"], parse_ip_neigh), (["arp", "-an"], parse_arp_an)):
        try:
            result = await transport.run(argv, timeout_s=timeout_s)
        except TransportError as exc:
            problems.append(f"{argv[0]}: {exc}")
            continue
        if result.ok:
            return parse(result.stdout)
        problems.append(f"{' '.join(argv)} exited {result.exit_code}: {result.tail(200)}")
    raise ArpReadError("could not read the ARP table — " + "; ".join(problems))


class ArpReadError(Exception):
    """Neither ``ip neigh`` nor ``arp -an`` could be run on the transport host."""


def parse_sweep_cidrs(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """``"192.168.1.0/24, 10.0.0.0/24"`` -> networks. Raises ValueError on a
    bad CIDR or one larger than :data:`MAX_SWEEP_HOSTS` addresses."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        net = ipaddress.ip_network(part, strict=False)
        if net.num_addresses > MAX_SWEEP_HOSTS + 2:
            raise ValueError(
                f"{part} has {net.num_addresses} addresses; the ARP sweep is capped at "
                f"{MAX_SWEEP_HOSTS} (a /22) per CIDR"
            )
        nets.append(net)
    return nets


def _ping_loop_script(hosts: list[str]) -> str:
    """A POSIX sh loop pinging ``hosts`` in parallel batches of SWEEP_BATCH.
    Every host is an ``ipaddress``-produced string, so nothing in the script
    comes from user text."""
    lines: list[str] = []
    for i in range(0, len(hosts), SWEEP_BATCH):
        batch = " ".join(hosts[i : i + SWEEP_BATCH])
        lines.append(f"for h in {batch}; do ping -c1 -W1 $h >/dev/null 2>&1 & done; wait")
    lines.append("exit 0")
    return "\n".join(lines)


async def sweep(
    transport: CommandTransport,
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> list[str]:
    """ "ARP ping" each network so the neighbour table is populated before it is
    read. ``fping -a -q -g`` when it exists, else a bounded parallel ``ping``
    loop. Best effort: returns the problems it hit, never raises."""
    problems: list[str] = []
    for net in networks:
        cidr = str(net)
        try:
            result = await transport.run(
                ["fping", "-a", "-q", "-r", "1", "-t", "500", "-g", cidr], timeout_s=60
            )
            # 0 = all alive, 1 = some unreachable, 2 = some address not found:
            # all mean fping ran, which is all a sweep needs.
            if result.exit_code in (0, 1, 2):
                continue
            if result.exit_code != _COMMAND_NOT_FOUND:
                log.debug("ipdiscovery.sweep.fping_failed", cidr=cidr, rc=result.exit_code)
        except TransportError as exc:
            log.debug("ipdiscovery.sweep.no_fping", cidr=cidr, error=str(exc))

        hosts = [str(h) for h in list(net.hosts())[:MAX_SWEEP_HOSTS]]
        batches = (len(hosts) + SWEEP_BATCH - 1) // SWEEP_BATCH
        try:
            result = await transport.run(
                ["sh", "-c", _ping_loop_script(hosts)], timeout_s=15 + 3 * batches
            )
        except TransportError as exc:
            problems.append(f"sweep of {cidr} failed: {exc}")
            continue
        if not result.ok:
            problems.append(f"sweep of {cidr} exited {result.exit_code}: {result.tail(200)}")
    return problems


__all__ = [
    "MAX_SWEEP_HOSTS",
    "ArpReadError",
    "MacTable",
    "parse_arp_an",
    "parse_ip_neigh",
    "parse_sweep_cidrs",
    "read_arp_table",
    "sweep",
]
