"""Turn guest readings into alerts.

Each check is one small class with one job: look at one :class:`GuestFacts` and say what state
that guest is in for this one concern, as a notification :class:`Observation`. The
notification engine does the rest (debounce, rules, "recovered"), exactly as for disks.
A check that has nothing to say about a guest (no agent, stopped, not known) returns None, and
that guest's last state for it is left as it was.

Adding a check (network, sign-ins, frozen after a backup) is a new class in :data:`CHECKS`:
nothing else changes. All checks use level ``agent`` and the guest's key, with a suffix for
all but the first so each concern keeps its own state (``node/vmid``,
``node/vmid/lockdown``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlmodel import Session, select

from orchestrator.db.models import Node, Service, TargetState
from orchestrator.domain.enums import GuestKind
from orchestrator.guests.model import AgentState, GuestFacts
from orchestrator.notify.engine import Observation

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


#: Every check, in the order they are announced.
CHECKS: tuple[GuestCheck, ...] = (AnswersCheck(), LockdownCheck())


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
    "GUEST_ALERT_LEVELS",
    "LEVEL",
    "AnswersCheck",
    "GuestCheck",
    "LockdownCheck",
    "Scope",
    "observations",
    "open_alerts",
    "scopes",
]
