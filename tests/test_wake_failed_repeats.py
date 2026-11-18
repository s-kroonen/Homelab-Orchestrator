"""Traffic keeps waking a service that cannot start: one wake_failed alert says so, not one
every few minutes. The repeats stay in the feed, marked as held back."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from orchestrator.db.models import AuditEntry, NotificationEvent
from orchestrator.pipelines.wake import repeated_wake_failure

ERROR = "'web' did not become HEALTHY within the wake timeout: http returned 502"


def _sent(
    session: Session, body: str = ERROR, *, hours_ago: float = 0.5, held: bool = False
) -> None:
    session.add(NotificationEvent(level="service", target="web", event="wake_failed",
                                  body=body, suppressed=held,
                                  at=datetime.now(UTC) - timedelta(hours=hours_ago)))  # fmt: skip
    session.commit()


def test_the_first_wake_failure_is_sent(session: Session) -> None:
    assert repeated_wake_failure(session, "web", ERROR) == ""


def test_the_same_failure_again_is_held_back(session: Session) -> None:
    _sent(session)
    why = repeated_wake_failure(session, "web", ERROR)
    assert "same wake failure was sent at" in why and "after 24 hours" in why
    # held-back repeats do not reset the clock: still measured from the one that was sent
    _sent(session, hours_ago=0.1, held=True)
    assert repeated_wake_failure(session, "web", ERROR) != ""


def test_a_different_error_is_sent(session: Session) -> None:
    _sent(session)
    assert repeated_wake_failure(session, "web", "Portainer answered 502") == ""


def test_after_a_wake_worked_a_new_failure_is_sent(session: Session) -> None:
    _sent(session, hours_ago=2)
    session.add(AuditEntry(actor="system", action="wake.succeeded", target="service:web",
                           created_at=datetime.now(UTC) - timedelta(hours=1)))  # fmt: skip
    session.commit()
    assert repeated_wake_failure(session, "web", ERROR) == ""


def test_a_day_later_it_is_sent_again(session: Session) -> None:
    _sent(session, hours_ago=25)
    assert repeated_wake_failure(session, "web", ERROR) == ""


def test_another_service_is_its_own(session: Session) -> None:
    _sent(session)
    assert repeated_wake_failure(session, "api", ERROR) == ""


async def test_announce_records_a_held_back_event_without_sending(session: Session) -> None:
    from typing import Any

    from orchestrator.notify import hooks

    seen: list[Any] = []

    class Engine:
        async def emit(self, _session: Session, event: Any) -> None:
            seen.append(event)

    hooks.install(Engine())  # type: ignore[arg-type]
    try:
        await hooks.announce(session, level="service", key="web", name="Web", event="wake_failed",
                             severity="warn", title="t", body=ERROR, suppressed="repeat")  # fmt: skip
    finally:
        hooks.install(None)
    assert seen[0].tags == ["service", "suppressed:repeat"]
