"""Activity timeline: one merged, newest-first feed of what the orchestrator did.

Nothing new is stored for this — it reads the two tables that already record
every privileged action:

* :class:`PipelineRun` — each wake/backup, with its per-step record;
* :class:`AuditEntry` — who asked for what, and how it ended.

A run and the audit rows it produced share a ``correlation_id``
(``wake.start`` / ``wake.succeeded`` and the run row are the same event seen
twice). Those audit rows are folded INTO the run event rather than listed
beside it: the feed would otherwise say everything two or three times, and
the run row alone cannot say who triggered it — only its ``*.start`` audit
row carries the actor.

Health verdicts are deliberately absent: the health engine overwrites
``ServiceState`` rather than appending history, so there is no timeline to
show. The *current* per-probe breakdown is on the main dashboard instead,
next to each service's health badge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, col, desc, select

from orchestrator.db.models import AuditEntry, PipelineRun, Service
from orchestrator.web.auth import require_admin
from orchestrator.web.deps import SessionDep
from orchestrator.web.routers.backup import _run_to_dict

router = APIRouter(prefix="/api", tags=["activity"], dependencies=[Depends(require_admin)])

#: Hard ceiling on the feed. A wake writes 3 rows and a run of steps; 100 events
#: is weeks of activity on a homelab, and the page renders all of it inline.
MAX_EVENTS = 100


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite drops tzinfo on the way back out; everything is written as UTC.
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _sort_key(event: dict[str, Any]) -> str:
    return event["at"] or ""


def _audit_to_dict(entry: AuditEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "actor": entry.actor,
        "action": entry.action,
        "target": entry.target,
        "result": entry.result.value,
        "details": entry.details,
        "correlation_id": entry.correlation_id,
        "at": _iso(entry.created_at),
    }


def build_feed(
    session: Session,
    *,
    service: str | None = None,
    group: str | None = None,
    limit: int = MAX_EVENTS,
) -> list[dict[str, Any]]:
    """Newest-first list of events, each ``{"type": "run" | "audit", ...}``.

    ``service`` narrows to one slug: runs by ``service_id``, audit rows by
    their ``service:<slug>`` target. Audit rows with no service target (logins,
    invites) therefore drop out of a filtered feed, which is the point of it.

    ``group`` narrows the same way but to every service carrying that group
    tag (purely organisational — see ``Service.group``); combining both filters
    narrows to their intersection.
    """
    limit = max(1, min(limit, MAX_EVENTS))
    all_services = session.exec(select(Service)).all()
    slugs = {s.id: s.slug for s in all_services}

    run_stmt = select(PipelineRun).order_by(desc(PipelineRun.started_at)).limit(limit)
    audit_stmt = select(AuditEntry).order_by(desc(AuditEntry.created_at)).limit(limit)
    if service is not None:
        service_id = next((sid for sid, slug in slugs.items() if slug == service), None)
        # An unknown slug must match NO runs — `== None` would compile to IS NULL
        # and return every run without a service instead.
        run_stmt = run_stmt.where(PipelineRun.service_id == (service_id or -1))
        audit_stmt = audit_stmt.where(AuditEntry.target == f"service:{service}")
    if group is not None:
        group_service_ids = [s.id for s in all_services if s.group == group]
        group_targets = [f"service:{s.slug}" for s in all_services if s.group == group]
        # An unknown/empty group must match NO runs, same reasoning as above —
        # `in_([])` compiles to a tautological-false clause, which is correct.
        run_stmt = run_stmt.where(col(PipelineRun.service_id).in_(group_service_ids))
        audit_stmt = audit_stmt.where(col(AuditEntry.target).in_(group_targets))
    runs = session.exec(run_stmt).all()

    # Every audit row belonging to a run in the window, even an older one — a
    # long backup's ``backup.start`` can predate the newest ``limit`` audits.
    run_cids = [r.correlation_id for r in runs]
    related: dict[str, list[AuditEntry]] = {}
    if run_cids:
        for entry in session.exec(
            select(AuditEntry)
            .where(col(AuditEntry.correlation_id).in_(run_cids))
            .order_by(col(AuditEntry.created_at))
        ).all():
            related.setdefault(entry.correlation_id or "", []).append(entry)

    events: list[dict[str, Any]] = []
    for run in runs:
        audits = related.get(run.correlation_id, [])
        event = _run_to_dict(run)
        event.update(
            {
                "type": "run",
                "at": _iso(run.started_at),
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "service": slugs.get(run.service_id) if run.service_id is not None else None,
                # The *.start row is written first, so the earliest actor is the trigger.
                "actor": audits[0].actor if audits else None,
                "audit": [_audit_to_dict(a) for a in audits],
            }
        )
        events.append(event)

    folded = set(run_cids)
    for entry in session.exec(audit_stmt).all():
        if entry.correlation_id in folded:
            continue
        event = _audit_to_dict(entry)
        event["type"] = "audit"
        target = entry.target
        event["service"] = (
            target.removeprefix("service:") if target.startswith("service:") else None
        )
        events.append(event)

    events.sort(key=_sort_key, reverse=True)
    return events[:limit]


@router.get("/activity")
async def activity(
    session: SessionDep,
    service: str | None = Query(None, description="Only events for this service slug"),
    group: str | None = Query(None, description="Only events for services tagged with this group"),
    limit: int = Query(MAX_EVENTS, ge=1, le=MAX_EVENTS),
) -> list[dict[str, Any]]:
    return build_feed(session, service=service, group=group, limit=limit)
