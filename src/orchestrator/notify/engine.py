"""The notification engine.

Feed it observations ("service nginx is FAILED", "node pve1 is DOWN") on every
scan; it announces CHANGES, and only the useful ones:

* **Debounced.** A change must be seen on ``notify_confirm_scans`` consecutive
  scans before it is announced (one blip is not an incident).
* **Grouped under the root cause.** An observation can name the incident that
  explains it (``cause_key``: ``node:pve1``, ``guest:pve1/101``). While that cause
  is itself failed/down, the effect is recorded in the feed as suppressed instead
  of paging you once per container on a dead host.
* **Flap-damped.** ``notify_flap_threshold`` changes inside ``notify_flap_window_s``
  = one "flapping" alert, then silence until it settles.
* **Rule-driven.** Rules (level / scope / event / severity / channel) decide who
  hears about it; with no rules at all, failed / down / recovered go to every
  channel.

UNKNOWN ("we could not look") is never announced and never counts as a change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.notifier.base import Notifier, NotifySeverity
from orchestrator.config import Settings
from orchestrator.db.models import NotificationEvent, NotificationRule, TargetState
from orchestrator.logging_setup import get_logger
from orchestrator.notify.links import absolute, link_for

log = get_logger(__name__)

LEVELS = ("node", "guest", "service", "sub")
EVENTS = (
    "failed",
    "down",
    "recovered",
    "flapping",
    "recovery_attempt",
    "recovery_succeeded",
    "recovery_exhausted",
    "restore_proposed",
    "restore_succeeded",
    "restore_failed",
    "mcp_request",
    "dump_failed",
    "dump_succeeded",
    "backup_failed",
    "backup_retry",
    "backup_skipped",
    "wake_failed",
    "power_restore_failed",
    "update_succeeded",
    "update_rolled_back",
    "update_failed",
    "switch_failed",
    "outside_change",
    "deploy_succeeded",
    "deploy_failed",
    "deploy_blocked",
    "deploy_outside",
    "test",
)
SEVERITIES = ("info", "warn", "error", "critical")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}
#: What a rule-less install announces.
DEFAULT_EVENTS = (
    "failed",
    "down",
    "recovered",
    "flapping",
    "backup_failed",
    "backup_retry",
    "wake_failed",
    "power_restore_failed",
    "update_succeeded",
    "update_rolled_back",
    "update_failed",
    "switch_failed",
    "outside_change",
    "deploy_failed",
    "deploy_blocked",
    "recovery_succeeded",
    "recovery_exhausted",
    "dump_failed",
    "mcp_request",
    "restore_proposed",
    "restore_succeeded",
    "restore_failed",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass
class Observation:
    level: str  # node | guest | service | sub
    key: str  # slug, node name, or "node/vmid"
    name: str
    state: str  # healthy | failed | down | unknown
    reason: str = ""
    #: The incident that explains this one ("node:pve1"); see the module docstring.
    cause_key: str | None = None
    scope: dict[str, str] = field(default_factory=dict)  # slug / group / node


@dataclass
class Event:
    level: str
    key: str
    name: str
    event: str
    severity: str
    title: str
    body: str
    cause_key: str | None = None
    scope: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    approve_url: str | None = None


def _severity_for(level: str, state: str) -> str:
    if state == "failed":
        return "error"
    if state == "down":
        return "error" if level == "node" else "warn"
    return "info"


class NotificationEngine:
    def __init__(self, notifier: Notifier, settings: Settings) -> None:
        self._notifier = notifier
        self._settings = settings

    # -- state changes -----------------------------------------------------------

    async def observe(
        self, session: Session, observations: Sequence[Observation]
    ) -> list[NotificationEvent]:
        """Apply one scan's observations (cause targets first!) and send what
        became worth saying."""
        if not self._settings.notifications_enabled:
            return []
        out: list[NotificationEvent] = []
        for obs in observations:
            event = self._advance(session, obs)
            if event is None:
                continue
            out.append(await self._deliver(session, event))
        session.commit()
        return out

    def _advance(self, session: Session, obs: Observation) -> Event | None:
        row = session.get(TargetState, (obs.level, obs.key))
        now = _now()
        if row is None:
            # First sight: remember it, announce nothing.
            session.add(TargetState(level=obs.level, key=obs.key, state=obs.state, since=now))
            return None
        row.updated_at = now
        if obs.state == "unknown" or obs.state == row.state:
            row.pending_state, row.pending_count = "", 0
            session.add(row)
            return None

        if obs.state == row.pending_state:
            row.pending_count += 1
        else:
            row.pending_state, row.pending_count = obs.state, 1
        if row.pending_count < max(1, self._settings.notify_confirm_scans):
            session.add(row)
            return None

        previous = row.state
        row.state, row.since = obs.state, now
        row.pending_state, row.pending_count = "", 0
        window = timedelta(seconds=self._settings.notify_flap_window_s)
        recent = [
            t
            for t in (datetime.fromisoformat(x) for x in row.transitions or [])
            if now - (_aware(t) or now) <= window
        ]
        recent.append(now)
        row.transitions = [t.isoformat() for t in recent]

        muted_until = _aware(row.muted_until)
        if muted_until is not None and now < muted_until:
            session.add(row)
            return self._suppressed(obs, previous, "flapping - muted until it settles")
        if len(recent) >= max(2, self._settings.notify_flap_threshold):
            row.muted_until = now + window
            row.announced = "flapping"
            session.add(row)
            return Event(
                level=obs.level,
                key=obs.key,
                name=obs.name,
                event="flapping",
                severity="warn",
                title=f"{obs.name} is flapping",
                body=f"{obs.name} changed state {len(recent)} times in the last "
                f"{self._settings.notify_flap_window_s // 60} minutes (now {obs.state}). "
                "Further changes are muted until it settles.",
                scope=obs.scope,
                tags=[obs.level],
            )
        session.add(row)

        if obs.state == "healthy":
            if not row.announced:
                return None  # nobody was told it was bad, so nobody needs "recovered"
            row.announced = ""
            return Event(
                level=obs.level,
                key=obs.key,
                name=obs.name,
                event="recovered",
                severity="info",
                title=f"{obs.name} recovered",
                body=f"{obs.name} is healthy again (was {previous}).",
                scope=obs.scope,
                tags=[obs.level],
            )
        word = obs.state.upper()
        event = Event(
            level=obs.level,
            key=obs.key,
            name=obs.name,
            event=obs.state,
            severity=_severity_for(obs.level, obs.state),
            title=f"{obs.name} is {word}",
            body=(obs.reason or f"{obs.name} is {obs.state}.")[:1500],
            cause_key=obs.cause_key,
            scope=obs.scope,
            tags=[obs.level],
        )
        if obs.cause_key:
            level, _, key = obs.cause_key.partition(":")
            cause = session.get(TargetState, (level, key))
            if cause is not None and cause.state in ("failed", "down"):
                return self._suppressed_event(event, f"covered by {obs.cause_key}")
        row.announced = obs.state
        return event

    @staticmethod
    def _suppressed(obs: Observation, previous: str, reason: str) -> Event:
        return NotificationEngine._suppressed_event(
            Event(
                level=obs.level,
                key=obs.key,
                name=obs.name,
                event=obs.state if obs.state != "healthy" else "recovered",
                severity=_severity_for(obs.level, obs.state),
                title=f"{obs.name}: {previous} -> {obs.state}",
                body=obs.reason,
                scope=obs.scope,
            ),
            reason,
        )

    @staticmethod
    def _suppressed_event(event: Event, reason: str) -> Event:
        event.tags = [*event.tags, f"suppressed:{reason}"]
        return event

    # -- explicit events (recovery, backups, restores, test) ---------------------

    async def emit(self, session: Session, event: Event) -> NotificationEvent:
        row = await self._deliver(session, event)
        session.commit()
        return row

    async def emit_direct(
        self, session: Session, event: Event, *, channels: Sequence[str] | None = None
    ) -> NotificationEvent:
        """Send to exactly the named channels (None = all), ignoring rules - the
        test button, which must show what each channel does."""
        row = NotificationEvent(
            level=event.level,
            target=event.key,
            name=event.name,
            event=event.event,
            severity=event.severity,
            title=event.title,
            body=event.body,
        )
        row.delivered = await self._notifier.notify_via(
            list(channels) if channels else None,
            severity=NotifySeverity(event.severity),
            title=event.title,
            body=event.body,
            tags=event.tags,
            approve_url=self._open_url(event),
        )
        if not self._notifier.channels():
            row.suppressed, row.suppressed_reason = True, "no channel is active"
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    # -- delivery ----------------------------------------------------------------

    def _open_url(self, event: Event) -> str | None:
        """What tapping the alert opens: the approval page when there is one, else the
        page of the thing it is about."""
        return event.approve_url or absolute(
            self._settings, link_for(event.level, event.key, event.event)
        )

    def _rules(self, session: Session) -> list[NotificationRule]:
        return list(session.exec(select(NotificationRule).order_by(NotificationRule.id)).all())

    @staticmethod
    def _rule_matches(rule: NotificationRule, event: Event) -> bool:
        if not rule.enabled:
            return False
        if rule.levels and event.level not in rule.levels:
            return False
        if rule.events and event.event not in rule.events:
            return False
        if _RANK.get(event.severity, 0) < _RANK.get(rule.min_severity, 0):
            return False
        scope = rule.scope or {}
        if scope.get("slugs") or scope.get("groups") or scope.get("nodes"):
            hit = (
                event.key in (scope.get("slugs") or [])
                or event.scope.get("slug", "") in (scope.get("slugs") or [])
                or event.scope.get("group", "") in (scope.get("groups") or [])
                or event.scope.get("node", "") in (scope.get("nodes") or [])
            )
            if not hit:
                return False
        return True

    async def _deliver(self, session: Session, event: Event) -> NotificationEvent:
        suppressed_reason = next(
            (t.removeprefix("suppressed:") for t in event.tags if t.startswith("suppressed:")), ""
        )
        row = NotificationEvent(
            level=event.level,
            target=event.key,
            name=event.name,
            event=event.event,
            severity=event.severity,
            title=event.title,
            body=event.body,
            cause=event.cause_key or "",
            suppressed=bool(suppressed_reason),
            suppressed_reason=suppressed_reason,
        )
        if not suppressed_reason:
            rules = self._rules(session)
            if rules:
                matched = [r for r in rules if self._rule_matches(r, event)]
                channels: set[str] | None = set()
                for r in matched:
                    if not r.channels:
                        channels = None
                        break
                    channels |= set(r.channels)
                row.rule_ids = [r.id for r in matched if r.id is not None]
                send = bool(matched)
                if not send:
                    row.suppressed, row.suppressed_reason = True, "no rule wants this"
            else:
                send = event.event in DEFAULT_EVENTS or event.event == "test"
                channels = None
                if not send:
                    row.suppressed, row.suppressed_reason = True, "not announced by default"
            if send:
                row.delivered = await self._notifier.notify_via(
                    sorted(channels) if channels else None,
                    severity=NotifySeverity(event.severity),
                    title=event.title,
                    body=event.body,
                    tags=event.tags,
                    approve_url=self._open_url(event),
                )
        session.add(row)
        session.flush()
        log.info(
            "notify.event",
            level=event.level,
            target=event.key,
            kind=event.event,
            suppressed=row.suppressed,
            delivered=row.delivered,
        )
        return row

    # -- housekeeping ------------------------------------------------------------

    def prune(self, session: Session) -> int:
        cutoff = _now() - timedelta(days=max(1, self._settings.notify_retention_days))
        old = session.exec(
            select(NotificationEvent).where(col(NotificationEvent.at) < cutoff)
        ).all()
        for row in old:
            session.delete(row)
        return len(old)


def summarize_delivery(row: NotificationEvent) -> dict[str, Any]:
    """The feed's view of one event."""
    return {
        "id": row.id,
        "at": _aware(row.at).isoformat() if row.at else None,
        "level": row.level,
        "target": row.target,
        "name": row.name,
        "event": row.event,
        "severity": row.severity,
        "title": row.title,
        "body": row.body,
        "cause": row.cause,
        "suppressed": row.suppressed,
        "suppressed_reason": row.suppressed_reason,
        "delivered": row.delivered or {},
        "link": link_for(row.level, row.target, row.event),
    }
