"""Turn guest readings into alerts.

Each check is one small class with one job: look at one :class:`GuestFacts` and say what state
that guest is in for this one concern, as a notification :class:`Observation`. The
notification engine does the rest (debounce, rules, "recovered"), exactly as for disks.
A check that has nothing to say about a guest (no agent, stopped, not known) returns None, and
that guest's last state for it is left as it was.

Adding a check is a new class in :data:`CHECKS`: nothing else changes. All checks use level
``agent`` and the guest's key, with a suffix for all but the first so each concern keeps its
own state (``node/vmid``, ``node/vmid/lockdown``, ``node/vmid/network``,
``node/vmid/frozen``).

Some things are moments, not states: someone signed in, someone signed out, the address
changed. Those are :class:`GuestEventCheck` classes in :data:`EVENT_CHECKS`: they compare a
guest's reading with its previous one and say what happened in between, as notification
events (sent once, routed by the same rules).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlmodel import Session, select

from orchestrator.db.models import Node, Service, TargetState
from orchestrator.domain.enums import GuestKind
from orchestrator.guests.model import AgentState, GuestFacts, GuestRef, SignedIn, parse_time
from orchestrator.notify.engine import Event, Observation

LEVEL = "agent"


@dataclass(frozen=True)
class Scope:
    """Where a guest sits, for the rules and the alert text."""

    node: str = ""
    slug: str = ""
    group: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"node": self.node, "slug": self.slug, "group": self.group}


class GuestCheck(Protocol):
    def observe(self, facts: GuestFacts, scope: Scope) -> Observation | None: ...


def _title_name(facts: GuestFacts) -> str:
    return facts.name or facts.ref.key


class AnswersCheck:
    """The agent stopped answering on a guest where it answered before (or is enabled):
    the guest runs, but its operating system may be stuck. Announced after the engine's usual
    confirmation, so one slow answer is not an alert."""

    def observe(self, facts: GuestFacts, scope: Scope) -> Observation | None:
        if not facts.running or facts.agent not in (AgentState.ANSWERS, AgentState.NOT_ANSWERING):
            return None
        bad = facts.agent is AgentState.NOT_ANSWERING
        name = f"Guest agent on {_title_name(facts)}"
        return Observation(
            level=LEVEL,
            key=facts.ref.key,
            name=name,
            state="agent_unresponsive" if bad else "healthy",
            reason=(
                f"{_title_name(facts)} is running but its guest agent does not answer "
                f"({facts.agent_detail}). Its address, disks and sign-ins are not being read."
                if bad
                else ""
            ),
            scope=scope.as_dict(),
            title=f"{name} is not answering" if bad else None,
        )


class LockdownCheck:
    """The agent lets commands, file access or password changes through. The orchestrator
    never sends them, but whoever holds a Proxmox token with more rights could."""

    def observe(self, facts: GuestFacts, scope: Scope) -> Observation | None:
        if facts.agent is not AgentState.ANSWERS or facts.locked_down is None:
            return None
        open_ = facts.dangerous_enabled
        name = f"Guest agent on {_title_name(facts)}"
        return Observation(
            level=LEVEL,
            key=f"{facts.ref.key}/lockdown",
            name=name,
            state="lockdown_weakened" if open_ else "healthy",
            reason=(
                f"The guest agent on {_title_name(facts)} allows {', '.join(open_)}. Block them "
                "in /etc/qemu/qemu-ga.conf (block-rpcs=...) and restart qemu-guest-agent; see "
                "the guest's setup steps."
                if open_
                else ""
            ),
            scope=scope.as_dict(),
            title=f"{name} allows commands" if open_ else None,
        )


class NetworkCheck:
    """The guest runs and its agent answers, but its own network card (the MAC its source
    knows) is gone or has no address: nothing can reach it over the network."""

    def observe(self, facts: GuestFacts, scope: Scope) -> Observation | None:
        if facts.agent is not AgentState.ANSWERS or facts.interfaces is None:
            return None
        known = set(facts.macs)
        own = [i for i in facts.interfaces if i.mac and i.mac in known]
        watched = own or facts.interfaces
        down = not any(i.addresses for i in watched)
        name = f"Network of {_title_name(facts)}"
        if own:
            what = ", ".join(f"{i.name} ({i.mac})" for i in own)
            why = f"its network card {what} has no address"
        elif known:
            why = f"no interface with its MAC ({', '.join(sorted(known))}) is up inside it"
        else:
            why = "no interface inside it has an address"
        return Observation(
            level=LEVEL,
            key=f"{facts.ref.key}/network",
            name=name,
            state="network_down" if down else "healthy",
            reason=(
                f"{_title_name(facts)} is running and its agent answers, but {why}. Nothing "
                "can reach it over the network."
                if down
                else ""
            ),
            scope=scope.as_dict(),
            severity="error" if down else None,
            title=f"{name} is down" if down else None,
        )


class FrozenCheck:
    """The guest's filesystems are frozen: writes are paused, usually by a backup that did
    not thaw them again. Checked every cycle and right after the orchestrator's backups."""

    def observe(self, facts: GuestFacts, scope: Scope) -> Observation | None:
        if facts.agent is not AgentState.ANSWERS or facts.freeze not in ("frozen", "thawed"):
            return None
        frozen = facts.freeze == "frozen"
        node, _, vmid = facts.ref.key.partition("/")
        name = f"Filesystems of {_title_name(facts)}"
        return Observation(
            level=LEVEL,
            key=f"{facts.ref.key}/frozen",
            name=name,
            state="guest_frozen" if frozen else "healthy",
            reason=(
                f"The filesystems inside {_title_name(facts)} are frozen: nothing it writes "
                "is saved until they are thawed. A backup usually thaws them; this one did "
                f"not. Thaw them on {node or 'its node'}: qm guest cmd {vmid or '<vmid>'} "
                "fsfreeze-thaw"
                if frozen
                else ""
            ),
            scope=scope.as_dict(),
            severity="error" if frozen else None,
            title=f"{name} are frozen" if frozen else None,
        )


#: Every check, in the order they are announced.
CHECKS: tuple[GuestCheck, ...] = (AnswersCheck(), LockdownCheck(), NetworkCheck(), FrozenCheck())


# -- moments ------------------------------------------------------------------------------------


class GuestEventCheck(Protocol):
    def events(
        self, previous: GuestFacts | None, current: GuestFacts, scope: Scope
    ) -> list[Event]: ...


def duration(seconds: float) -> str:
    """``12 min``, ``2 h 14 min``, ``3 d 4 h``, as the screens say it."""
    s = max(0, int(seconds))
    if s < 60:
        return "under a minute"
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d} d" + (f" {h} h" if h else "")
    if h:
        return f"{h} h" + (f" {m} min" if m else "")
    return f"{m} min"


def _who(s: SignedIn) -> str:
    return f"{s.user}@{s.domain}" if s.domain else s.user


def _event(facts: GuestFacts, scope: Scope, event: str, title: str, body: str) -> Event:
    return Event(
        level=LEVEL,
        key=facts.ref.key,
        name=_title_name(facts),
        event=event,
        severity="info",
        title=title,
        body=body,
        scope=scope.as_dict(),
        tags=[LEVEL],
    )


class SignInEvents:
    """Someone signed in to the guest, or signed out (with how long they were signed in).
    Only between two readings that both listed who is signed in, so the first reading after
    the agent was set up, or came back, announces nobody. A sign-out is known to within one
    reading."""

    def events(self, previous: GuestFacts | None, current: GuestFacts, scope: Scope) -> list[Event]:
        if previous is None or previous.sessions is None or current.sessions is None:
            return []
        key = lambda s: (s.user, s.domain, s.since)  # noqa: E731
        before = {key(s): s for s in previous.sessions}
        after = {key(s): s for s in current.sessions}
        name = _title_name(current)
        now = parse_time(current.read_at)
        out: list[Event] = []
        for k in sorted(set(before) - set(after)):
            s = before[k]
            took = duration(s.seconds(now)) if now else ""
            out.append(
                _event(
                    current,
                    scope,
                    "guest_signout",
                    f"{_who(s)} signed out of {name}",
                    f"{_who(s)} signed out of {name}"
                    + (f" after {took}" if took else "")
                    + f" (signed in since {s.since[:16].replace('T', ' ')} UTC).",
                )
            )
        for k in sorted(set(after) - set(before)):
            s = after[k]
            out.append(
                _event(
                    current,
                    scope,
                    "guest_signin",
                    f"{_who(s)} signed in to {name}",
                    f"{_who(s)} signed in to {name} at {s.since[:16].replace('T', ' ')} UTC, as "
                    "its guest agent reports it.",
                )
            )
        return out


class AddressEvents:
    """The guest's own address changed between two readings that both had one."""

    def events(self, previous: GuestFacts | None, current: GuestFacts, scope: Scope) -> list[Event]:
        if previous is None or not previous.address or not current.address:
            return []
        if previous.address == current.address:
            return []
        name = _title_name(current)
        return [
            _event(
                current,
                scope,
                "ip_changed",
                f"Address of {name} changed to {current.address}",
                f"{name} moved from {previous.address} to {current.address}, as its guest agent "
                "reports it. A service on it follows the new address.",
            )
        ]


#: Every moment check.
EVENT_CHECKS: tuple[GuestEventCheck, ...] = (SignInEvents(), AddressEvents())


def scopes(session: Session) -> dict[str, Scope]:
    """``node/vmid`` -> where that guest sits (its service, if one runs on it)."""
    nodes = {n.id: n.name for n in session.exec(select(Node)).all()}
    out: dict[str, Scope] = {}
    for svc in session.exec(select(Service).where(Service.guest_id != None)).all():  # noqa: E711
        if svc.guest_kind is GuestKind.NONE or svc.parent_id is not None:
            continue
        node = nodes.get(svc.node_id, "")
        out[f"{node}/{svc.guest_id}"] = Scope(node=node, slug=svc.slug, group=svc.group)
    return out


def events(
    session: Session,
    previous: dict[GuestRef, GuestFacts],
    readings: Iterable[GuestFacts],
    checks: Iterable[GuestEventCheck] = EVENT_CHECKS,
) -> list[Event]:
    """What happened to each guest since its previous reading."""
    where = scopes(session)
    checks = tuple(checks)
    out: list[Event] = []
    for facts in readings:
        scope = where.get(facts.ref.key) or Scope(node=facts.ref.key.partition("/")[0])
        before = previous.get(facts.ref)
        for check in checks:
            out.extend(check.events(before, facts, scope))
    return out


def observations(
    session: Session, readings: Iterable[GuestFacts], checks: Iterable[GuestCheck] = CHECKS
) -> list[Observation]:
    where = scopes(session)
    checks = tuple(checks)
    out: list[Observation] = []
    for facts in readings:
        scope = where.get(facts.ref.key) or Scope(node=facts.ref.key.partition("/")[0])
        for check in checks:
            obs = check.observe(facts, scope)
            if obs is not None:
                out.append(obs)
    return out


#: The alert levels that are about one guest (key ``node/vmid``, with or without a suffix).
GUEST_ALERT_LEVELS = ("guest", "disk", LEVEL)
_QUIET = {"healthy", "unknown", ""}


def open_alerts(session: Session, key: str) -> list[TargetState]:
    """The alerts about guest ``key`` (``node/vmid``) that are open right now."""
    rows = session.exec(
        select(TargetState).where(TargetState.level.in_(GUEST_ALERT_LEVELS))  # type: ignore[attr-defined]
    ).all()
    return [
        r
        for r in rows
        if (r.key == key or r.key.startswith(key + "/")) and r.state not in _QUIET and r.announced
    ]


__all__ = [
    "CHECKS",
    "EVENT_CHECKS",
    "GUEST_ALERT_LEVELS",
    "LEVEL",
    "AddressEvents",
    "AnswersCheck",
    "FrozenCheck",
    "GuestCheck",
    "GuestEventCheck",
    "LockdownCheck",
    "NetworkCheck",
    "Scope",
    "SignInEvents",
    "duration",
    "events",
    "observations",
    "open_alerts",
    "scopes",
]
