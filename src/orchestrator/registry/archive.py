"""Soft delete: what is deleted waits in the archive, then is purged.

Deleting a service (and the sub-services that go with it), a proxy host or a backup policy
takes a snapshot of it in the registry file's own shape and parks that in
:class:`ArchivedService` for ``ARCHIVE_RETENTION_DAYS``. Restoring writes the snapshot back
through the same code the YAML load uses, so a restored service is exactly what a load of
that YAML would make.

What is NOT restored is history that was never tied to the registry rows: backup records,
runs and health state keep pointing at nothing, as they do after any delete. Proxy hosts
come back un-pushed, so they reach Traefik Manager with the next Save/Push. A deleted
backup policy comes back with the services that were in it (those still without a policy);
the backup records that pointed at it stay detached.
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
from orchestrator.domain.enums import RouterProvider
from orchestrator.logging_setup import get_logger
from orchestrator.registry.backup_plan import clean_slug_list
from orchestrator.registry.host_cascade import host_served
from orchestrator.registry.loader import (
    _spec_from_db,
    apply_service_spec,
    policy_spec,
    proxy_host_spec,
)
from orchestrator.registry.proxy_push import take_pending_delete
from orchestrator.registry.schema import BackupPolicySpec, ProxyHostSpec, ServiceSpec
from orchestrator.registry.traefik_probes import sync_traefik_probe

log = get_logger(__name__)


KIND_SERVICE = "service"
KIND_PROXY_HOST = "proxy_host"
KIND_POLICY = "backup_policy"
KIND_LABEL = {
    KIND_SERVICE: "Service",
    KIND_PROXY_HOST: "Proxy host",
    KIND_POLICY: "Backup policy",
}


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


def archive_proxy_host(
    session: Session, settings: Settings, ph: ProxyHost, actor: str
) -> ArchivedService:
    """Snapshot a proxy host BEFORE it is deleted."""
    svc = session.get(Service, ph.service_id) if ph.service_id else None
    now = _now()
    row = ArchivedService(
        kind=KIND_PROXY_HOST,
        slug=ph.hostname,
        ref=str(ph.id),
        name=ph.hostname,
        parent_slug=svc.slug if svc else None,
        snapshot={
            "service_slug": svc.slug if svc else None,
            "proxy_host": proxy_host_spec(ph).model_dump(mode="json"),
        },
        deleted_at=now,
        deleted_by=actor,
        purge_at=now + timedelta(days=max(1, settings.archive_retention_days)),
    )
    session.add(row)
    return row


def archive_policy(
    session: Session,
    settings: Settings,
    policy: BackupPolicy,
    members: list[str],
    actor: str,
) -> ArchivedService:
    """Snapshot a backup policy and the services that were in it BEFORE it is deleted."""
    now = _now()
    row = ArchivedService(
        kind=KIND_POLICY,
        slug=policy.name,
        ref=policy.name,
        name=policy.name,
        snapshot={
            "policy": policy_spec(policy).model_dump(mode="json"),
            "members": list(members),
        },
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


def latest_for(session: Session, slug: str, kind: str = KIND_SERVICE) -> ArchivedService | None:
    """The most recent archive entry of ``kind`` for ``slug`` (it can have been deleted more
    than once); for a service this covers sub-services that went down with a parent too."""
    rows = [r for r in list_archive(session) if r.kind == kind]
    exact = next((r for r in rows if r.slug == slug), None)
    if kind != KIND_SERVICE:
        return exact
    return exact or next((r for r in rows if slug in (r.sub_slugs or [])), None)


def latest_ref(session: Session, kind: str, ref: str) -> ArchivedService | None:
    """By what an old link names: a proxy host's former id, a policy's name."""
    return next((r for r in list_archive(session) if r.kind == kind and r.ref == ref), None)


def describe(row: ArchivedService) -> dict[str, Any]:
    snap = row.snapshot or {}
    out: dict[str, Any] = {
        "id": row.id,
        "kind": row.kind,
        "kind_label": KIND_LABEL.get(row.kind, row.kind),
        "slug": row.slug,
        "name": row.name,
        "parent": row.parent_slug,
        "sub_services": list(row.sub_slugs or []),
        "probes": 0,
        "proxy_hosts": [],
        "page": "/archive",
        "deleted_at": _aware(row.deleted_at).isoformat(),
        "deleted_by": row.deleted_by,
        "purge_at": _aware(row.purge_at).isoformat(),
        "days_left": max(0, (_aware(row.purge_at) - _now()).days),
    }
    parts: list[str] = []
    if row.kind == KIND_PROXY_HOST:
        spec = snap.get("proxy_host") or {}
        out["page"] = "/proxy-hosts"
        parts.append(f"-> {spec.get('upstream') or 'no upstream'}")
        if snap.get("service_slug"):
            parts.append(f"service {snap['service_slug']}")
    elif row.kind == KIND_POLICY:
        spec = snap.get("policy") or {}
        out["page"] = "/backup-policies"
        parts.append(spec.get("schedule_cron") or "no schedule")
        members = snap.get("members") or []
        if members:
            parts.append(f"{len(members)} service(s): {', '.join(members)}")
    else:
        specs = snap.get("services") or []
        root = next((s for s in specs if s.get("slug") == row.slug), {})
        out["probes"] = len(root.get("probes") or [])
        out["proxy_hosts"] = [h.get("hostname") for h in root.get("proxy_hosts") or []]
        out["page"] = f"/services/{row.slug}"
        if row.sub_slugs:
            parts.append(f"{len(row.sub_slugs)} sub-service(s): {', '.join(row.sub_slugs)}")
        if out["probes"]:
            parts.append(f"{out['probes']} probe(s)")
        if out["proxy_hosts"]:
            parts.append(
                f"{len(out['proxy_hosts'])} proxy host(s): {', '.join(out['proxy_hosts'])}"
            )
    out["summary"] = " · ".join(parts)
    return out


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
    """Bring an archived service (and its sub-services), proxy host or backup policy back.
    Raises :class:`ArchiveError` when it cannot, changing nothing."""
    row = session.get(ArchivedService, archive_id)
    if row is None:
        raise ArchiveError("that archive entry is gone (purged, or already restored)")
    try:
        if row.kind == KIND_PROXY_HOST:
            return _restore_proxy_host(session, row, actor)
        if row.kind == KIND_POLICY:
            return _restore_policy(session, row, actor)
        return _restore_service(session, row, actor)
    except Exception:
        session.rollback()
        raise


def _restore_service(session: Session, row: ArchivedService, actor: str) -> Restored:
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

    return _restore_rows(session, row, specs, live, nodes, policies, actor)


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


def _restore_proxy_host(session: Session, row: ArchivedService, actor: str) -> Restored:
    from orchestrator.config import get_settings

    snap = row.snapshot or {}
    spec = ProxyHostSpec.model_validate(snap.get("proxy_host") or {})
    slug = snap.get("service_slug")
    svc = session.exec(select(Service).where(Service.slug == slug)).first() if slug else None
    if slug and svc is None:
        raise ArchiveError(
            f"its service {slug!r} no longer exists - restore the service first "
            "(it may still be in the archive)"
        )
    if session.exec(select(ProxyHost).where(ProxyHost.hostname == spec.hostname)).first():
        raise ArchiveError(f"{spec.hostname} is used by a proxy host again - delete that one first")
    ph = ProxyHost(
        service_id=svc.id if svc else None,
        hostname=spec.hostname,
        upstream=spec.upstream,
        router_provider=RouterProvider(spec.router_provider),
        extra=dict(spec.extra),
        wake_on_hit=spec.wake_on_hit,
        entry_points=list(spec.entry_points),
        cert_resolver=spec.cert_resolver,
        route_options=dict(spec.route_options),
        enabled=spec.enabled,
        pushed_sig="",
    )
    session.add(ph)
    session.flush()
    take_pending_delete(session, spec.hostname)  # its old route is wanted again, not removed
    sync_traefik_probe(
        session,
        get_settings(),
        hostname=ph.hostname,
        service_id=ph.service_id,
        enabled=host_served(session, ph),
    )
    audit.record(
        session,
        actor=actor,
        action="proxy_host.restored",
        target=f"proxy_host:{spec.hostname}",
        details={"id": ph.id, "service": slug},
    )
    session.delete(row)
    session.commit()
    log.info("archive.restored", kind=row.kind, hostname=spec.hostname)
    return Restored(slug=spec.hostname, restored=[spec.hostname])


def _restore_policy(session: Session, row: ArchivedService, actor: str) -> Restored:
    snap = row.snapshot or {}
    spec = BackupPolicySpec.model_validate(snap.get("policy") or {})
    if session.exec(select(BackupPolicy).where(BackupPolicy.name == spec.name)).first():
        raise ArchiveError(f"a backup policy named {spec.name!r} exists again - rename it first")
    live = {s.slug: s for s in session.exec(select(Service)).all()}
    warnings: list[str] = []
    requires = [r for r in spec.requires if r in live]
    for gone in sorted(set(spec.requires) - set(requires)):
        warnings.append(f"dropped required service {gone!r} (no longer exists)")
    policy = BackupPolicy(
        name=spec.name,
        schedule_cron=spec.schedule_cron,
        mode=spec.mode,
        retention=dict(spec.retention),
        targets=dict(spec.targets),
        requires=requires,
        wake_before_backup=spec.wake_before_backup,
        wake_mode=spec.wake_mode,
        max_attempts=spec.max_attempts,
        restore_power_state=spec.restore_power_state,
    )
    session.add(policy)
    session.flush()
    back: list[str] = []
    for member in snap.get("members") or []:
        svc = live.get(member)
        if svc is None:
            warnings.append(f"{member}: no longer exists")
        elif svc.backup_policy_id is not None:
            warnings.append(f"{member}: is in another policy now, left there")
        elif svc.backup_excluded:
            warnings.append(f"{member}: is marked never back up, left out")
        else:
            svc.backup_policy_id = policy.id
            session.add(svc)
            back.append(member)
    audit.record(
        session,
        actor=actor,
        action="backup_policy.restored",
        target=f"policy:{spec.name}",
        details={"services": back, "warnings": warnings},
    )
    session.delete(row)
    session.commit()
    log.info("archive.restored", kind=row.kind, policy=spec.name)
    return Restored(slug=spec.name, restored=[spec.name, *back], warnings=warnings)


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
    slug, kind = row.slug, row.kind
    session.delete(row)
    audit.record(session, actor=actor, action=f"{kind}.purged", target=f"{kind}:{slug}", details={})
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
    """An archive entry whose thing is live again (a Reset brought it back) is stale."""
    live = {
        KIND_SERVICE: set(session.exec(select(Service.slug)).all()),
        KIND_PROXY_HOST: set(session.exec(select(ProxyHost.hostname)).all()),
        KIND_POLICY: set(session.exec(select(BackupPolicy.name)).all()),
    }
    rows = [r for r in list_archive(session) if r.slug in live.get(r.kind, set())]
    for row in rows:
        session.delete(row)
    return len(rows)
