"""What the orchestrator knows about a guest from inside it, whoever told it.

A *guest* today is a Proxmox VM or container. Later it may be a plain host with an agent and
no Proxmox at all. Nothing in this module (or in the collector, store, checks and screens built
on it) assumes Proxmox: a guest is a :class:`GuestRef` (which source, which key), what can be
read about it is :class:`GuestFacts`, and who reads it is a :class:`GuestFactsProvider`. Only
``guests/providers/`` knows how a particular kind of source answers. A field a provider cannot
fill stays ``None``; the screens leave out what is ``None``. See docs/guest_agent.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from orchestrator.adapters.proxmox.agent import DANGEROUS_COMMANDS


class AgentState(StrEnum):
    """Can the guest's agent be read, and if not, why not."""

    ANSWERS = "answers"
    #: Enabled, but it did not answer (not installed, not running, or the guest is stuck).
    NOT_ANSWERING = "not_answering"
    #: The source says the agent is switched off for this guest (Proxmox: the VM option).
    NOT_ENABLED = "not_enabled"
    #: The source refused to ask it (Proxmox: the token lacks VM.GuestAgent.Audit).
    NO_PERMISSION = "no_permission"
    NOT_RUNNING = "not_running"
    #: The source reads this kind of guest itself (Proxmox containers).
    NOT_NEEDED = "not_needed"
    #: The source could not be asked at all this time.
    UNKNOWN = "unknown"


#: Short words for the screens and the assistants.
AGENT_LABELS: dict[AgentState, str] = {
    AgentState.ANSWERS: "answers",
    AgentState.NOT_ANSWERING: "not answering",
    AgentState.NOT_ENABLED: "not set up",
    AgentState.NO_PERMISSION: "no permission",
    AgentState.NOT_RUNNING: "guest stopped",
    AgentState.NOT_NEEDED: "not needed",
    AgentState.UNKNOWN: "unknown",
}


@dataclass(frozen=True)
class GuestRef:
    """Which guest: the source that lists it (a connection id; blank = that kind's default
    source) and its key there (Proxmox: ``node/vmid``)."""

    source: str
    key: str

    def __str__(self) -> str:
        return f"{self.source}:{self.key}"


@dataclass(frozen=True)
class GuestTarget:
    """A guest as its source lists it: enough to read it. ``node`` / ``vmid`` / ``kind`` are
    Proxmox's; another kind of source fills only what it has."""

    ref: GuestRef
    name: str
    running: bool
    kind: str = ""  # "vm" | "ct" | "host" ...
    node: str = ""
    vmid: int | None = None


@dataclass
class Interface:
    name: str
    mac: str = ""
    #: ``10.0.2.45/24``-style, as the guest reports them (loopback and link-local left out).
    addresses: list[str] = field(default_factory=list)
    rx_bytes: int | None = None
    tx_bytes: int | None = None


@dataclass
class SignedIn:
    """One user signed in to the guest. The QEMU agent reports one line per user, with that
    user's earliest sign-in: two sessions of the same user show as one."""

    user: str
    since: str  # ISO 8601, UTC
    domain: str = ""

    def seconds(self, now: datetime | None = None) -> int:
        start = _parse(self.since)
        if start is None:
            return 0
        return max(0, int(((now or datetime.now(UTC)) - start).total_seconds()))


@dataclass
class GuestFacts:
    """One reading of a guest. ``None`` = not known (not read, or the source cannot say)."""

    ref: GuestRef
    name: str = ""
    kind: str = ""
    running: bool = False
    read_at: str = ""
    agent: AgentState = AgentState.UNKNOWN
    #: Why the agent is not answering / cannot be read, in words.
    agent_detail: str = ""
    agent_version: str | None = None
    ping_ms: float | None = None
    os_name: str | None = None
    os_version: str | None = None
    kernel: str | None = None
    #: When the OS was last read (it is read once a day).
    os_read_at: str | None = None
    #: The source's own idea of the OS family ("l26", "win11" on Proxmox): picks setup steps.
    os_type: str | None = None
    macs: list[str] = field(default_factory=list)
    interfaces: list[Interface] | None = None
    #: Usable addresses, best first (the NIC whose MAC the source knows comes first).
    addresses: list[str] = field(default_factory=list)
    #: Who gave the addresses: "guest_agent" or "lxc".
    address_source: str | None = None
    sessions: list[SignedIn] | None = None
    #: Filesystems as ``{"mount", "fstype", "used", "total"}``.
    filesystems: list[dict[str, Any]] | None = None
    filesystems_error: str | None = None
    #: "thawed" | "frozen" (the agent's fsfreeze-status).
    freeze: str | None = None
    #: The source's own "freeze filesystems during backup" option for this guest.
    freeze_on_backup: bool | None = None
    #: The agent commands that are switched on (its ``info``).
    enabled_commands: list[str] | None = None
    #: Reads in a row the agent did not answer; drives the back-off and the alert.
    misses: int = 0
    #: Cycles skipped since the last read (back-off for a guest that keeps timing out).
    skipped: int = 0
    #: command -> what went wrong reading it (the rest of the reading still counts).
    errors: dict[str, str] = field(default_factory=dict)

    # -- derived ---------------------------------------------------------------------------

    @property
    def locked_down(self) -> bool | None:
        """True when the agent refuses commands, file access and password changes; None when
        it is not known (no ``info`` read)."""
        if self.enabled_commands is None:
            return None
        return not (set(self.enabled_commands) & DANGEROUS_COMMANDS)

    @property
    def dangerous_enabled(self) -> list[str]:
        return sorted(set(self.enabled_commands or []) & DANGEROUS_COMMANDS)

    @property
    def address(self) -> str:
        return self.addresses[0] if self.addresses else ""

    # -- storage ---------------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["ref"] = {"source": self.ref.source, "key": self.ref.key}
        out["agent"] = self.agent.value
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GuestFacts:
        data = dict(raw)
        ref = data.pop("ref", {}) or {}
        agent = data.pop("agent", AgentState.UNKNOWN.value)
        interfaces = data.pop("interfaces", None)
        sessions = data.pop("sessions", None)
        known = set(cls.__dataclass_fields__)
        facts = cls(
            ref=GuestRef(str(ref.get("source", "")), str(ref.get("key", ""))),
            **{k: v for k, v in data.items() if k in known},
        )
        try:
            facts.agent = AgentState(agent)
        except ValueError:
            facts.agent = AgentState.UNKNOWN
        if interfaces is not None:
            facts.interfaces = [Interface(**i) for i in interfaces if isinstance(i, dict)]
        if sessions is not None:
            facts.sessions = [SignedIn(**s) for s in sessions if isinstance(s, dict)]
        return facts


class GuestFactsProvider(Protocol):
    """Reads one guest. Never raises: what goes wrong is on the facts (``agent``,
    ``agent_detail``, ``errors``). ``os_due``: read the OS too (it is read daily)."""

    async def read(
        self, target: GuestTarget, previous: GuestFacts | None, *, os_due: bool
    ) -> GuestFacts: ...


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_time(value: str | None) -> datetime | None:
    return _parse(value)


__all__ = [
    "AGENT_LABELS",
    "AgentState",
    "GuestFacts",
    "GuestFactsProvider",
    "GuestRef",
    "GuestTarget",
    "Interface",
    "SignedIn",
    "now_iso",
    "parse_time",
]
