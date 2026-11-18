"""Background jobs that failed, as alerts.

The orchestrator does things on its own: prune old backups after the nightly run, back
itself up, save ``services.yaml``, push proxy hosts to Traefik Manager. When one of those
fails it used to land in the log only (a prune refused by PBS every night for weeks went
unnoticed while the datastore filled up). Now each failure is a **warning** at level
``job``, with the error as the adapter gave it (PBS's own reason included):

* ``job_failed`` when a job fails. The same failure again (same job, same error, nothing
  worked since) is kept in the feed but sent again only after 24 hours.
* ``recovered`` once, when that job works again.

A job is named ``<kind>:<subject>`` (``retention:haos``, ``autosave:yaml``,
``self_backup``); :func:`orchestrator.notify.links.link_for` opens the page for each kind.

This is for jobs the orchestrator *tried and could not finish*. Whether a connection is
reachable at all (Proxmox, PBS, Portainer, ...) is a separate, debounced question: a node
switched off on purpose makes its PBS unreachable every scan, which is not worth a warning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, desc, select

from orchestrator.db.models import NotificationEvent
from orchestrator.notify.hooks import announce

LEVEL = "job"
FAILED = "job_failed"
#: The same failure is sent again after this long at most.
REPEAT_QUIET = timedelta(hours=24)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _last(session: Session, job: str, event: str, *, sent_only: bool) -> NotificationEvent | None:
    stmt = select(NotificationEvent).where(
        NotificationEvent.level == LEVEL,
        NotificationEvent.target == job,
        NotificationEvent.event == event,
    )
    if sent_only:
        stmt = stmt.where(NotificationEvent.suppressed == False)  # noqa: E712 - SQL comparison
    return session.exec(stmt.order_by(desc(NotificationEvent.at))).first()


def repeat_reason(session: Session, job: str, body: str, now: datetime | None = None) -> str:
    """Why this failure is only recorded: the last one sent for ``job`` said the same, the
    job has not worked since, and it is less than :data:`REPEAT_QUIET` old. Else ""."""
    last = _last(session, job, FAILED, sent_only=True)
    if last is None or last.body != body:
        return ""
    sent = _aware(last.at)
    if (now or datetime.now(UTC)) - sent >= REPEAT_QUIET:
        return ""
    fixed = _last(session, job, "recovered", sent_only=False)
    if fixed is not None and _aware(fixed.at) > sent:
        return ""
    return (
        f"the same failure was sent at {sent:%H:%M} UTC and the job has not worked since: "
        "sent again when the error changes, it works again, or after 24 hours"
    )


def error_text(error: BaseException | str) -> str:
    """The error as an alert says it (adapter errors already carry the service's reason)."""
    text = str(error)
    if not text and isinstance(error, BaseException):
        text = type(error).__name__
    return text[:1500]


async def job_failed(
    session: Session,
    *,
    job: str,
    name: str,
    error: BaseException | str,
    impact: str = "",
    scope: dict[str, str] | None = None,
) -> None:
    """Announce that background job ``job`` (``<kind>:<subject>``) failed. Never raises."""
    body = error_text(error) + (f"\n{impact}" if impact else "")
    await announce(
        session,
        level=LEVEL,
        key=job,
        name=name,
        event=FAILED,
        severity="warn",
        title=f"{name} failed",
        body=body,
        scope=scope or {},
        suppressed=repeat_reason(session, job, body),
    )


async def job_ok(
    session: Session, *, job: str, name: str, scope: dict[str, str] | None = None
) -> None:
    """Background job ``job`` worked: announce ``recovered`` if it had failed since it last
    recovered (otherwise nothing: a job that works is not news). Never raises."""
    failed = _last(session, job, FAILED, sent_only=False)
    if failed is None:
        return
    fixed = _last(session, job, "recovered", sent_only=False)
    if fixed is not None and _aware(fixed.at) >= _aware(failed.at):
        return
    await announce(
        session,
        level=LEVEL,
        key=job,
        name=name,
        event="recovered",
        severity="info",
        title=f"{name} works again",
        body=f"{name} worked again (it had failed: {failed.body.splitlines()[0][:300]})",
        scope=scope or {},
    )


__all__ = ["FAILED", "LEVEL", "REPEAT_QUIET", "error_text", "job_failed", "job_ok", "repeat_reason"]
