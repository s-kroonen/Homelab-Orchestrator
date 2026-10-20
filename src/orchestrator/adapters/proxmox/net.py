"""Parsers for the network bits of Proxmox guest config and guest-agent replies.

Pure functions, no I/O — the live adapter fetches, these read. Kept apart so
the tests can feed them real-shaped payloads without an HTTP mock.

Shapes handled:

* QEMU config ``netN``: ``virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1`` —
  the first key is the NIC *model* (virtio, e1000, e1000e, rtl8139, vmxnet3,
  ...) and its value is the MAC. Older configs may carry ``macaddr=``.
* LXC config ``netN``: ``name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,
  ip=10.0.0.5/24,ip6=auto,type=veth``. A static ``ip=`` is itself an answer;
  ``ip=dhcp`` / ``ip=manual`` is not.
* QEMU guest agent ``network-get-interfaces``: ``{"result": [{"name": "eth0",
  "hardware-address": "bc:24:...", "ip-addresses": [{"ip-address-type":
  "ipv4", "ip-address": "10.0.0.5", "prefix": 24}]}]}``.
* LXC ``/interfaces``: ``[{"name": "eth0", "hwaddr": "bc:24:...", "inet":
  "10.0.0.5/24", "inet6": "fe80::1/64"}]`` (newer releases may also give an
  ``ip-addresses`` list like the agent's).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from typing import Any

_NET_KEY = re.compile(r"^net\d+$")
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_NULL_MAC = "00:00:00:00:00:00"


def normalize_mac(raw: object) -> str | None:
    """``AA-BB-CC-DD-EE-FF`` / ``aa:bb:..`` -> ``aa:bb:cc:dd:ee:ff``; None if not a MAC."""
    text = str(raw or "").strip().lower().replace("-", ":")
    if not _MAC.match(text) or text == _NULL_MAC:
        return None
    return text


def usable_ip(raw: object) -> str | None:
    """The address in canonical form if it is one a service could be reached
    on — not loopback, link-local, unspecified or multicast. Accepts a
    ``10.0.0.5/24`` CIDR suffix. None otherwise."""
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.split("/", 1)[0].split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast:
        return None
    return str(addr)


def _split_net_value(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in str(value).split(","):
        key, sep, val = part.partition("=")
        if sep:
            pairs.append((key.strip().lower(), val.strip()))
    return pairs


def _net_values(config: Mapping[str, Any]) -> list[str]:
    keys = sorted((k for k in config if _NET_KEY.match(str(k))), key=lambda k: int(str(k)[3:]))
    return [str(config[k]) for k in keys]


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def parse_qemu_net_macs(config: Mapping[str, Any]) -> list[str]:
    """MACs of every ``netN`` NIC in a QEMU VM config, in NIC order."""
    macs: list[str] = []
    for value in _net_values(config):
        for _key, val in _split_net_value(value):
            # The model key (virtio/e1000/...) carries the MAC; a stray
            # `macaddr=` is honoured too. Other values (bridge=vmbr0, tag=10)
            # are never MAC-shaped, so normalize_mac skips them.
            mac = normalize_mac(val)
            if mac:
                macs.append(mac)
                break
    return _dedupe(macs)


def qemu_agent_enabled(config: Mapping[str, Any]) -> bool:
    """``agent: 1`` / ``agent: enabled=1,fstrim_cloned_disks=1`` -> True.
    Missing or ``0`` -> False: asking a VM whose agent is not configured just
    burns a request on a guaranteed error."""
    raw = str(config.get("agent", "")).strip()
    if not raw:
        return False
    # Property string: the first item may be the bare default key's value
    # ("1"); otherwise `enabled=` must be spelled out, and absent means off.
    first = raw.split(",", 1)[0].strip()
    value = first if "=" not in first else dict(_split_net_value(raw)).get("enabled", "0")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def parse_lxc_net(config: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """``(macs, static_ips)`` from an LXC config's ``netN`` entries.

    A static ``ip=`` / ``ip6=`` (``10.0.0.5/24``) is a usable answer by itself;
    ``dhcp``, ``auto`` and ``manual`` are not.
    """
    macs: list[str] = []
    v4: list[str] = []
    v6: list[str] = []
    for value in _net_values(config):
        fields = dict(_split_net_value(value))
        mac = normalize_mac(fields.get("hwaddr"))
        if mac:
            macs.append(mac)
        ip4 = usable_ip(fields.get("ip"))
        if ip4:
            v4.append(ip4)
        ip6 = usable_ip(fields.get("ip6"))
        if ip6:
            v6.append(ip6)
    return _dedupe(macs), _dedupe(v4 + v6)


def _rank(entries: list[tuple[str | None, str]], macs: list[str]) -> list[str]:
    """Order (mac, ip) pairs: MAC-matched IPv4, MAC-matched IPv6, then the
    rest. When any NIC matched a config MAC, the unmatched ones (docker0,
    veths, VPN tunnels) are dropped — they are not how the LAN reaches it."""
    wanted = set(macs)
    matched = [ip for mac, ip in entries if mac and mac in wanted]
    others = [ip for mac, ip in entries if not (mac and mac in wanted)]
    pool = matched if matched else others

    def v4_first(ips: list[str]) -> list[str]:
        return [ip for ip in ips if ":" not in ip] + [ip for ip in ips if ":" in ip]

    return _dedupe(v4_first(pool))


def parse_agent_interfaces(payload: Any, macs: list[str]) -> list[str]:
    """IPs from a guest agent ``network-get-interfaces`` reply, best first."""
    rows = payload.get("result") if isinstance(payload, Mapping) else payload
    entries: list[tuple[str | None, str]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("name", "")).strip() == "lo":
            continue
        mac = normalize_mac(row.get("hardware-address"))
        for addr in row.get("ip-addresses") or []:
            if not isinstance(addr, Mapping):
                continue
            ip = usable_ip(addr.get("ip-address"))
            if ip:
                entries.append((mac, ip))
    return _rank(entries, macs)


def parse_lxc_interfaces(rows: Any, macs: list[str]) -> list[str]:
    """IPs from ``GET /nodes/{node}/lxc/{vmid}/interfaces``, best first."""
    entries: list[tuple[str | None, str]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("name", "")).strip() == "lo":
            continue
        mac = normalize_mac(row.get("hwaddr") or row.get("hardware-address"))
        candidates: list[object] = [row.get("inet"), row.get("inet6")]
        for addr in row.get("ip-addresses") or []:
            if isinstance(addr, Mapping):
                candidates.append(addr.get("ip-address"))
        for cand in candidates:
            # Some releases join several addresses with spaces/commas.
            for piece in re.split(r"[\s,]+", str(cand or "")):
                ip = usable_ip(piece)
                if ip:
                    entries.append((mac, ip))
    return _rank(entries, macs)


__all__ = [
    "normalize_mac",
    "parse_agent_interfaces",
    "parse_lxc_interfaces",
    "parse_lxc_net",
    "parse_qemu_net_macs",
    "qemu_agent_enabled",
    "usable_ip",
]
