"""Fire-and-forget access to the app's notification engine from pipelines.

The lifespan installs the engine; code that has no handle on the app (the backup
and wake pipelines, the recovery loop) announces through :func:`announce`. It never
raises: an alert that cannot be sent must not turn a failed backup into a crash.
"""

from __future__ import annotations

from sqlmodel import Session

from orchestrator.logging_setup import get_logger
from orchestrator.notify.engine import Event, NotificationEngine

log = get_logger(__name__)

_engine: NotificationEngine | None = None


def install(engine: NotificationEngine | None) -> None:
    global _engine
    _engine = engine


def current() -> NotificationEngine | None:
    return _engine


async def announce(
    session: Session,
    *,
    level: str,
    key: str,
    name: str,
    event: str,
    severity: str,
    title: str,
    body: str,
    scope: dict[str, str] | None = None,
    approve_url: str | None = None,
) -> None:
    engine = _engine
    if engine is None:
        return
    try:
        await engine.emit(
            session,
            Event(
                level=level,
                key=key,
                name=name,
                event=event,
                severity=severity,
                title=title,
                body=body,
                scope=scope or {},
                tags=[level],
                approve_url=approve_url,
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("notify.announce_failed", error=str(exc), error_type=type(exc).__name__)
