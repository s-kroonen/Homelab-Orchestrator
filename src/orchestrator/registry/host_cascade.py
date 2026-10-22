"""Which proxy hosts are actually served, given the service tree.

A host is served only when its own switch is on AND its service and every
ancestor of that service are enabled. The host's own ``enabled`` flag is never
rewritten by this, so enabling a service again brings back exactly the hosts
that were on before.
"""

from __future__ import annotations

from sqlmodel import Session, select

from orchestrator.config import Settings
from orchestrator.db.models import ProxyHost, Service
from orchestrator.registry.traefik_probes import ProbeSyncEvent, sync_traefik_probe
from orchestrator.registry.tree import TreeIndex


def tree_map(session: Session) -> dict[int, bool]:
    """``{proxy_host.id: its service and every ancestor are enabled}`` - the host's
    own switch is deliberately NOT part of it (callers AND it in, so a switch
    flipped mid-request is never shadowed by a stale snapshot)."""
    index = TreeIndex.build(session)
    out: dict[int, bool] = {}
    for ph in session.exec(select(ProxyHost)).all():
        if ph.id is None:
            continue
        svc = index.by_id.get(ph.service_id) if ph.service_id is not None else None
        out[ph.id] = svc is None or index.is_served(svc)
    return out


def host_served(session: Session, ph: ProxyHost) -> bool:
    if not ph.enabled:
        return False
    if ph.service_id is None:
        return True
    svc = session.get(Service, ph.service_id)
    if svc is None:
        return True
    return TreeIndex.build(session).is_served(svc) if svc.parent_id else svc.enabled


def resync_subtree_probes(
    session: Session, settings: Settings, service: Service
) -> list[ProbeSyncEvent]:
    """Re-sync the auto-managed ``traefik:<host>`` probe of every host under
    ``service`` (itself included) to the served state."""
    index = TreeIndex.build(session)
    ids = [service.id, *(d.id for d in index.descendants(service))]
    events: list[ProbeSyncEvent] = []
    for ph in session.exec(select(ProxyHost).where(ProxyHost.service_id.in_(ids))).all():  # type: ignore[attr-defined]
        svc = index.by_id.get(ph.service_id)
        served = ph.enabled and (svc is None or index.is_served(svc))
        events += sync_traefik_probe(
            session, settings, hostname=ph.hostname, service_id=ph.service_id, enabled=served
        )
    return events
