"""Soft delete: a deleted service waits in the archive, then is purged.

Deleting a service (and the sub-services that go with it) takes a snapshot of it in the
registry file's own shape and parks that in :class:`ArchivedService` for
``ARCHIVE_RETENTION_DAYS``. Restoring writes the snapshot back through the same code the
YAML load uses, so a restored service is exactly what a load of that YAML would make.

What is NOT restored is history that was never tied to the registry rows: backup records,
runs and health state keep pointing at nothing, as they do after any delete. Proxy hosts
come back un-pushed, so they reach Traefik Manager with the next Save/Push.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import (
    ArchivedService,
    BackupPolicy,
    Node,
    ProxyHost,
    Service,
)
from orchestrator.logging_setup import get_logger
from orchestrator.registry.backup_plan import clean_slug_list
from orchestrator.registry.loader import _spec_from_db, apply_service_spec
from orchestrator.registry.schema import ServiceSpec

log = get_logger(__name__)


class ArchiveError(Exception):
    """Restoring is not possible as things stand; the message says why."""


@dataclass
class Restored:
    slug: str
    restored: list[str]
    warnings: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# -- archiving ---------------------------------------------------------------------------


def archive_services(
    session: Session,
    settings: Settings,
    root: Service,
    below: list[Service],
    actor: str,
) -> ArchivedService:
    """Snapshot ``root`` and ``below`` (its sub-services) BEFORE they are deleted."""
    slugs = [root.slug, *(c.slug for c in below)]
    specs = {s.slug: s for s in _spec_from_db(session).services}
    parent_slug = specs[root.slug].parent
    now = _now()
    row = ArchivedService(
        slug=root.slug,
        name=root.name,
        parent_slug=parent_slug,
        sub_slugs=[c.slug for c in below],
        snapshot={"services": [specs[s].model_dump(mode="json") for s in slugs if s in specs]},
        deleted_at=now,
        deleted_by=actor,
        purge_at=now + timedelta(days=max(1, settings.archive_retention_days)),
    )
    session.add(row)
    return row


# -- reading -----------------------------------------------------------------------------


def list_archive(session: Session) -> list[ArchivedService]:
    return list(
        session.exec(select(ArchivedService).order_by(col(ArchivedService.deleted_at).desc())).all()
    )


def latest_for(session: Session, slug: str) -> ArchivedService | None:
    """The most recent archive entry for ``slug`` (it can have been deleted more than once),
    covering sub-services that went down with a parent too."""
    rows = list_archive(session)
    exact = next((r for r in rows if r.slug == slug), None)
    return exact or next((r for r in rows if slug in (r.sub_slugs or [])), None)


def describe(row: ArchivedService) -> dict[str, Any]:
    specs = (row.snapshot or {}).get("services") or []
    root = next((s for s in specs if s.get("slug") == row.slug), {})
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "parent": row.parent_slug,
        "sub_services": list(row.sub_slugs or []),
        "probes": len(root.get("probes") or []),
        "proxy_hosts": [h.get("hostname") for h in root.get("proxy_hosts") or []],
        "deleted_at": _aware(row.deleted_at).isoformat(),
        "deleted_by": row.deleted_by,
        "purge_at": _aware(row.purge_at).isoformat(),
        "days_left": max(0, (_aware(row.purge_at) - _now()).days),
    }


# -- restoring ---------------------------------------------------------------------------


def _depth_order(specs: list[ServiceSpec]) -> list[ServiceSpec]:
    """Parents before children."""
    by_slug = {s.slug: s for s in specs}

    def depth(spec: ServiceSpec) -> int:
        d, cur = 0, spec
        while cur.parent in by_slug and d < 32:
            cur, d = by_slug[cur.parent], d + 1
        return d

    return sorted(specs, key=lambda s: (depth(s), s.slug))


def restore(session: Session, archive_id: int, actor: str) -> Restored:
    """Bring an archived service (and its sub-services) back. Raises
    :class:`ArchiveError` when it cannot, changing nothing."""
    row = session.get(ArchivedService, archive_id)
    if row is None:
        raise ArchiveError("that archive entry is gone (purged, or already restored)")
    specs = _depth_order(
        [ServiceSpec.model_validate(s) for s in (row.snapshot or {}).get("services") or []]
    )
    if not specs:
        raise ArchiveError("the archive entry is empty")

    live = {s.slug: s for s in session.exec(select(Service)).all()}
    taken = [s.slug for s in specs if s.slug in live]
    if taken:
        raise ArchiveError(
            f"{', '.join(taken)} exist(s) again - rename or delete the live one first"
        )
    incoming = {s.slug for s in specs}
    for spec in specs:
        if spec.parent and spec.parent not in live and spec.parent not in incoming:
            raise ArchiveError(
                f"{spec.slug!r} was a sub-service of {spec.parent!r}, which no longer exists"
            )
    nodes = {n.name: n for n in session.exec(select(Node)).all()}
    policies = {p.name: p for p in session.exec(select(BackupPolicy)).all()}
    for spec in specs:
        if spec.parent is None and spec.node not in nodes:
            raise ArchiveError(f"node {spec.node!r} no longer exists - add it first")

    try:
        return _restore_rows(session, row, specs, live, nodes, policies, actor)
    except Exception:
        session.rollback()
        raise


def _restore_rows(
    session: Session,
    row: ArchivedService,
    specs: list[ServiceSpec],
    live: dict[str, Service],
    nodes: dict[str, Node],
    policies: dict[str, BackupPolicy],
    actor: str,
) -> Restored:
    incoming = {s.slug for s in specs}
    warnings: list[str] = []
    hostnames = set(session.exec(select(ProxyHost.hostname)).all())
    by_slug = {s.slug: s for s in specs}
    known = set(live) | incoming
    rows: dict[str, Service] = dict(live)
    for spec in specs:
        policy = policies.get(spec.backup_policy) if spec.backup_policy else None
        if spec.backup_policy and policy is None:
            warnings.append(f"{spec.slug}: backup policy {spec.backup_policy!r} is gone - none set")
        for dep in (*spec.depends_on, *spec.backup_requires):
            if dep not in known:
                warnings.append(f"{spec.slug}: dropped {dep!r} (no longer a service)")
        spec.depends_on = [d for d in spec.depends_on if d in known]
        spec.backup_requires = clean_slug_list([d for d in spec.backup_requires if d in known])
        for host in spec.proxy_hosts:
            if host.hostname in hostnames and host.enabled:
                host.enabled = False
                warnings.append(f"{host.hostname}: already used by another service - restored off")
            hostnames.add(host.hostname)

        top = spec
        while top.parent in by_slug:
            top = by_slug[top.parent]
        if top.parent is None:
            node = nodes.get(top.node)
        else:  # the top of this snapshot is a sub-service of a live parent: same node as it
            live_parent = session.get(Node, rows[top.parent].node_id)
            node = live_parent
        if node is None:
            raise ArchiveError(f"cannot place {spec.slug!r}: its node no longer exists")
        svc = Service(slug=spec.slug, name=spec.name)
        session.add(svc)
        apply_service_spec(session, svc, spec, node=node, policy=policy, pushed_sigs={})
        if spec.parent:
            svc.parent_id = rows[spec.parent].id
        rows[spec.slug] = svc
        session.flush()

    restored = [s.slug for s in specs]
    audit.record(
        session,
        actor=actor,
        action="service.restored",
        target=f"service:{row.slug}",
        details={
            "restored": restored,
            "warnings": warnings,
            "deleted_at": row.deleted_at.isoformat(),
        },
    )
    session.delete(row)
    session.commit()
    log.info("archive.restored", slug=row.slug, services=restored)
    return Restored(slug=row.slug, restored=restored, warnings=warnings)


# -- purging -----------------------------------------------------------------------------


def purge_expired(session: Session, now: datetime | None = None) -> int:
    cutoff = now or _now()
    rows = [r for r in list_archive(session) if _aware(r.purge_at) <= cutoff]
    for row in rows:
        session.delete(row)
    session.commit()
    if rows:
        log.info("archive.purged", count=len(rows))
    return len(rows)


def purge_now(session: Session, archive_id: int, actor: str) -> str:
    """Delete one entry for good ("empty it now")."""
    row = session.get(ArchivedService, archive_id)
    if row is None:
        raise ArchiveError("that archive entry is gone")
    slug = row.slug
    session.delete(row)
    audit.record(
        session, actor=actor, action="service.purged", target=f"service:{slug}", details={}
    )
    session.commit()
    return slug


# -- removed by a registry reload --------------------------------------------------------


def archive_removed(
    session: Session, settings: Settings, removed: list[str], actor: str
) -> list[ArchivedService]:
    """Snapshot services a registry (re)load is about to drop - a Reset, an older file put
    back - so a mistake there is as recoverable as a delete from the dashboard. One entry
    per top of a removed tree; its sub-services ride along."""
    if not removed:
        return []
    gone = set(removed)
    rows = {s.slug: s for s in session.exec(select(Service)).all()}
    by_id = {s.id: s for s in rows.values()}
    out: list[ArchivedService] = []
    for slug in sorted(gone):
        svc = rows.get(slug)
        if svc is None:
            continue
        parent = by_id.get(svc.parent_id) if svc.parent_id is not None else None
        if parent is not None and parent.slug in gone:
            continue  # goes with its parent's entry
        below: list[Service] = []
        frontier = [svc]
        while frontier:
            kids = [r for r in rows.values() if r.parent_id in {f.id for f in frontier}]
            below += [k for k in kids if k.slug in gone]
            frontier = [k for k in kids if k.slug in gone]
        out.append(archive_services(session, settings, svc, below, actor))
    return out


def drop_revived(session: Session) -> int:
    """An archive entry whose service is live again (a Reset brought it back) is stale."""
    live = set(session.exec(select(Service.slug)).all())
    rows = [r for r in list_archive(session) if r.slug in live]
    for row in rows:
        session.delete(row)
    return len(rows)
