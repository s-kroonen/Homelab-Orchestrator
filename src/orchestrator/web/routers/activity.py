"""Activity timeline: one merged, newest-first feed of what the orchestrator did.

Nothing new is stored for this — it reads the tables that already record
everything that happens:

* :class:`PipelineRun` — each wake/backup, with its per-step record;
* :class:`AuditEntry` — who asked for what, and how it ended;
* :class:`NotificationEvent` — the alerts the notification engine decided on
  (a node went down, a service failed, a backup could not run).

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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlmodel import Session, col, desc, select

from orchestrator.db.models import AuditEntry, Node, NotificationEvent, PipelineRun, Service
from orchestrator.domain.enums import AuditResult, GuestKind, PipelineKind, PipelineStatus
from orchestrator.registry.tree import TreeIndex
from orchestrator.web.auth import require_admin
from orchestrator.web.deps import SessionDep
from orchestrator.web.routers.backup import _run_to_dict

router = APIRouter(prefix="/api", tags=["activity"], dependencies=[Depends(require_admin)])

#: Hard ceiling on the feed; the page renders all of it inline.
MAX_EVENTS = 200
DEFAULT_EVENTS = 100

#: What the "kind" filter accepts: a pipeline kind, ``admin`` (what people did:
#: audit rows that belong to no run) or ``alert`` (notification events).
RUN_KINDS = tuple(k.value for k in PipelineKind if k is not PipelineKind.SCAN)
KINDS = (*RUN_KINDS, "admin", "alert")

#: Alert events and severities that count as "a problem" for the problems-only filter.
_PROBLEM_SEVERITIES = ("warn", "error", "critical")


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


def _alert_to_dict(row: NotificationEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "type": "alert",
        "at": _iso(row.at),
        "level": row.level,
        "target": row.target,
        "name": row.name,
        "event": row.event,
        "severity": row.severity,
        "title": row.title,
        "body": row.body,
        "suppressed": row.suppressed,
        "suppressed_reason": row.suppressed_reason,
        "delivered": row.delivered or {},
        "service": row.target if row.level in ("service", "sub") else None,
    }


@dataclass
class Scope:
    """Which services / node / guest a filtered feed is about."""

    slugs: set[str]
    ids: set[int]
    node: str | None
    guest: str | None  # "node/vmid"

    def audit_targets(self) -> list[str]:
        out = [f"service:{s}" for s in sorted(self.slugs)]
        if self.node:
            out.append(f"node:{self.node}")
        if self.guest:
            out.append(f"guest:{self.guest}")
        return out


def _scope(
    session: Session,
    *,
    service: str | None,
    group: str | None,
    node: str | None,
    guest: str | None,
) -> Scope | None:
    """The services the filters select (their intersection), or None when no
    filter narrows by place at all."""
    if not (service or group or node or guest):
        return None
    tree = TreeIndex.build(session)
    node_row = session.exec(select(Node).where(Node.name == node)).one_or_none() if node else None
    guest_node, _, guest_vmid = (guest or "").partition("/")
    chosen: list[Service] = []
    for svc in tree.by_slug.values():
        if service and svc.slug != service:
            continue
        if group and svc.group != group:
            continue
        root = tree.root(svc)  # a sub-service lives where its root does
        if node and (node_row is None or root.node_id != node_row.id):
            continue
        if guest:
            if (
                root.guest_kind is GuestKind.NONE
                or str(root.guest_id) != guest_vmid
                or tree.by_id.get(root.id or -1) is None
            ):
                continue
            root_node = session.get(Node, root.node_id) if root.node_id else None
            if root_node is None or root_node.name != guest_node:
                continue
        chosen.append(svc)
    return Scope(
        slugs={s.slug for s in chosen},
        ids={s.id for s in chosen if s.id is not None},
        node=node,
        guest=guest,
    )


def build_feed(
    session: Session,
    *,
    service: str | None = None,
    group: str | None = None,
    node: str | None = None,
    guest: str | None = None,
    kind: str | None = None,
    problems: bool = False,
    since_hours: int | None = None,
    q: str | None = None,
    limit: int = DEFAULT_EVENTS,
) -> list[dict[str, Any]]:
    """Newest-first list of events, each ``{"type": "run" | "audit" | "alert", ...}``.

    Place filters (``service``, ``group``, ``node``, ``guest`` as ``node/vmid``)
    combine as an intersection: the events about those services - and, for a node
    or guest, about the node or guest itself (``node:<name>`` / ``guest:<n>/<id>``
    audit rows and node/guest alerts). ``kind`` narrows the sort of event
    (``wake``, ``backup``, ``restore``, ``switch``, ``deploy``, ``admin``, ``alert``); ``problems`` keeps only
    what failed; ``since_hours`` only the recent past; ``q`` is a text search over
    the action, target, error and alert text. Audit rows with no service target
    (logins, invites) drop out as soon as a place filter is set.
    """
    limit = max(1, min(limit, MAX_EVENTS))
    scope = _scope(session, service=service, group=group, node=node, guest=guest)
    slugs = {s.id: s.slug for s in session.exec(select(Service)).all()}
    cutoff = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours else None
    text = (q or "").strip()
    events: list[dict[str, Any]] = []

    want_runs = kind in (None, *RUN_KINDS)
    want_audit = kind in (None, "admin") or kind in RUN_KINDS
    want_alerts = kind in (None, "alert")

    # -- pipeline runs --------------------------------------------------------
    runs: list[PipelineRun] = []
    run_cids: list[str] = []
    if want_runs:
        stmt = select(PipelineRun).order_by(desc(PipelineRun.started_at)).limit(limit)
        if kind in RUN_KINDS:
            stmt = stmt.where(PipelineRun.kind == PipelineKind(kind))
        if scope is not None:
            # An empty id set matches NO runs (`in_([])` is a false clause), which
            # is what an unknown slug / group / node should do.
            stmt = stmt.where(col(PipelineRun.service_id).in_(scope.ids))
        if cutoff is not None:
            stmt = stmt.where(col(PipelineRun.started_at) >= cutoff)
        if problems:
            stmt = stmt.where(PipelineRun.status == PipelineStatus.FAILED)
        if text:
            stmt = stmt.where(col(PipelineRun.error).contains(text))
        runs = list(session.exec(stmt).all())
        run_cids = [r.correlation_id for r in runs]

    # Every audit row belonging to a run in the window, even an older one — a
    # long backup's ``backup.start`` can predate the newest ``limit`` audits.
    related: dict[str, list[AuditEntry]] = {}
    if run_cids:
        for entry in session.exec(
            select(AuditEntry)
            .where(col(AuditEntry.correlation_id).in_(run_cids))
            .order_by(col(AuditEntry.created_at))
        ).all():
            related.setdefault(entry.correlation_id or "", []).append(entry)

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

    # -- audit rows that are not part of a run ----------------------------------
    if want_audit and kind not in RUN_KINDS:
        audit_stmt = select(AuditEntry).order_by(desc(AuditEntry.created_at)).limit(limit)
        if scope is not None:
            audit_stmt = audit_stmt.where(col(AuditEntry.target).in_(scope.audit_targets()))
        if cutoff is not None:
            audit_stmt = audit_stmt.where(col(AuditEntry.created_at) >= cutoff)
        if problems:
            audit_stmt = audit_stmt.where(AuditEntry.result != AuditResult.OK)
        if text:
            audit_stmt = audit_stmt.where(
                or_(
                    col(AuditEntry.action).contains(text),
                    col(AuditEntry.target).contains(text),
                    col(AuditEntry.actor).contains(text),
                )
            )
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

    # -- alerts: what the notification engine decided on --------------------------
    if want_alerts:
        stmt_a = select(NotificationEvent).order_by(desc(NotificationEvent.at)).limit(limit)
        if scope is not None:
            clauses: list[Any] = []
            if scope.slugs:
                clauses.append(
                    col(NotificationEvent.level).in_(("service", "sub"))
                    & col(NotificationEvent.target).in_(scope.slugs)
                )
            if scope.node:
                clauses.append(
                    (NotificationEvent.level == "node") & (NotificationEvent.target == scope.node)
                )
                clauses.append(
                    (NotificationEvent.level == "guest")
                    & col(NotificationEvent.target).startswith(f"{scope.node}/")
                )
            if scope.guest:
                clauses.append(
                    (NotificationEvent.level == "guest") & (NotificationEvent.target == scope.guest)
                )
            stmt_a = stmt_a.where(or_(*clauses)) if clauses else stmt_a.where(False)
        if cutoff is not None:
            stmt_a = stmt_a.where(col(NotificationEvent.at) >= cutoff)
        if problems:
            stmt_a = stmt_a.where(
                col(NotificationEvent.severity).in_(_PROBLEM_SEVERITIES)
                & (NotificationEvent.suppressed == False)  # noqa: E712
            )
        if text:
            stmt_a = stmt_a.where(
                or_(
                    col(NotificationEvent.title).contains(text),
                    col(NotificationEvent.body).contains(text),
                    col(NotificationEvent.target).contains(text),
                    col(NotificationEvent.event).contains(text),
                )
            )
        events.extend(_alert_to_dict(row) for row in session.exec(stmt_a).all())

    events.sort(key=_sort_key, reverse=True)
    return events[:limit]


def filter_options(session: Session) -> dict[str, Any]:
    """What the page offers in its dropdowns."""
    services = session.exec(select(Service).order_by(Service.slug)).all()
    nodes = [n.name for n in session.exec(select(Node).order_by(Node.name)).all()]
    node_by_id = {n.id: n.name for n in session.exec(select(Node)).all()}
    tree = TreeIndex.build(session)
    guests: dict[str, str] = {}
    for svc in services:
        root = tree.root(svc)
        if root.guest_kind is GuestKind.NONE or root.guest_id is None:
            continue
        node_name = node_by_id.get(root.node_id or -1)
        if node_name:
            key = f"{node_name}/{root.guest_id}"
            guests.setdefault(key, f"{root.guest_kind.value} {root.guest_id} on {node_name}")
    return {
        "slugs": [s.slug for s in services],
        "groups": sorted({s.group for s in services if s.group}),
        "nodes": nodes,
        "guests": [{"key": k, "label": v} for k, v in sorted(guests.items())],
        "kinds": list(KINDS),
    }


@router.get("/activity")
async def activity(
    session: SessionDep,
    service: str | None = Query(None, description="Only events for this service slug"),
    group: str | None = Query(None, description="Only events for services tagged with this group"),
    node: str | None = Query(None, description="Only events for this node (and what runs on it)"),
    guest: str | None = Query(None, description="Only events for this guest, as node/vmid"),
    kind: str | None = Query(
        None, description="wake | backup | restore | switch | deploy | admin | alert"
    ),
    problems: bool = Query(False, description="Only what failed"),
    since_hours: int | None = Query(None, ge=1, le=24 * 365),
    q: str | None = Query(None, description="Text to look for"),
    limit: int = Query(DEFAULT_EVENTS, ge=1, le=MAX_EVENTS),
) -> list[dict[str, Any]]:
    return build_feed(
        session,
        service=service or None,
        group=group or None,
        node=node or None,
        guest=guest or None,
        kind=kind if kind in KINDS else None,
        problems=problems,
        since_hours=since_hours,
        q=q,
        limit=limit,
    )
