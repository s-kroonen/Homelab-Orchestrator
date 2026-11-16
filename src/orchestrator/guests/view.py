"""What the screens and assistants get about a guest, from its reading. Source-independent:
a Proxmox guest and (later) a plain host look the same here; the source adds its own section
(``extra``) next to it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orchestrator.guests import setup_guide
from orchestrator.guests.model import AGENT_LABELS, AgentState, GuestFacts

#: How each address source is named next to an address that did not come from the agent.
ADDRESS_SOURCES = {
    "guest_agent": "agent",
    "lxc": "LXC",
    "arp": "ARP",
    "dhcp": "DHCP",
    "manual": "manual",
}

_NEEDS_A_LOOK = {AgentState.NOT_ANSWERING, AgentState.NOT_ENABLED, AgentState.NO_PERMISSION}


def os_text(facts: GuestFacts) -> str:
    name = (facts.os_name or "").replace(" GNU/Linux", "")
    return " ".join(x for x in (name, facts.os_version or "") if x)


def sessions(facts: GuestFacts, now: datetime | None = None) -> list[dict[str, Any]] | None:
    if facts.sessions is None:
        return None
    now = now or datetime.now(UTC)
    rows = [
        {"user": s.user, "domain": s.domain, "since": s.since, "seconds": s.seconds(now)}
        for s in facts.sessions
    ]
    return sorted(rows, key=lambda r: r["seconds"])


def summary(facts: GuestFacts | None, fallback_address: dict[str, str] | None = None) -> dict:
    """One row's worth: agent state, address, who is signed in, OS, freeze, lock-down."""
    if facts is None:
        address = fallback_address or {}
        return {
            "state": "unknown",
            "label": "not read yet",
            "detail": "read on the next health cycle",
            "needs_look": False,
            "address": address or None,
            "sessions": None,
            "os": "",
            "freeze": None,
            "locked_down": None,
            "read_at": None,
        }
    address: dict[str, str] | None = None
    if facts.address:
        source = facts.address_source or ""
        address = {
            "ip": facts.address,
            "source": source,
            "source_label": ADDRESS_SOURCES.get(source, source),
        }
    elif fallback_address:
        address = fallback_address
    return {
        "state": facts.agent.value,
        "label": AGENT_LABELS.get(facts.agent, facts.agent.value),
        "detail": facts.agent_detail,
        "needs_look": facts.agent in _NEEDS_A_LOOK or facts.locked_down is False,
        "address": address,
        "sessions": sessions(facts),
        "os": os_text(facts),
        "freeze": facts.freeze,
        "locked_down": facts.locked_down,
        "dangerous": facts.dangerous_enabled,
        "read_at": facts.read_at,
    }


def detail(facts: GuestFacts | None, fallback_address: dict[str, str] | None = None) -> dict:
    """Everything for the drawer: the summary, the readings, and the setup guide."""
    out = summary(facts, fallback_address)
    if facts is None:
        out["guide"] = None
        return out
    out.update(
        {
            "agent_version": facts.agent_version,
            "ping_ms": facts.ping_ms,
            "kernel": facts.kernel,
            "os_type": facts.os_type,
            "interfaces": (
                [
                    {
                        "name": i.name,
                        "mac": i.mac,
                        "addresses": i.addresses,
                        "rx_bytes": i.rx_bytes,
                        "tx_bytes": i.tx_bytes,
                        "up": bool(i.addresses),
                    }
                    for i in facts.interfaces
                ]
                if facts.interfaces is not None
                else None
            ),
            "filesystems": facts.filesystems,
            "filesystems_error": facts.filesystems_error,
            "freeze_on_backup": facts.freeze_on_backup,
            "errors": facts.errors,
            "guide": setup_guide.build(facts).as_dict(),
        }
    )
    return out


__all__ = ["ADDRESS_SOURCES", "detail", "os_text", "sessions", "summary"]
