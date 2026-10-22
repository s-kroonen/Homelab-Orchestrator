"""Turn one health-scan cycle into observations for the notification engine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session, select

from orchestrator.db.models import Node
from orchestrator.domain.enums import GuestKind, HealthState
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.notify.engine import Observation
from orchestrator.registry.tree import TreeIndex

_STATE = {
    HealthState.HEALTHY: "healthy",
    HealthState.FAILED: "failed",
    HealthState.DOWN: "down",
    HealthState.UNKNOWN: "unknown",
}


def _guest_details(verdict: ServiceVerdict) -> dict[str, Any] | None:
    """The guest gate's details from a verdict (its first result), if there was one."""
    first = verdict.probe_results[0] if verdict.probe_results else None
    if first is None or first.kind != "guest":  # type: ignore[comparison-overlap]
        return None
    return dict(first.details or {})


def collect(
    session: Session,
    verdicts: Mapping[str, ServiceVerdict],
    cluster: Any | None,
    tree: TreeIndex,
) -> list[Observation]:
    """Observations in cause-first order: nodes, guests, root services, sub-services."""
    out: list[Observation] = []
    nodes = {n.id: n for n in session.exec(select(Node)).all()}

    # -- nodes: what the Proxmox cluster says -------------------------------
    if cluster is not None:
        online = {n.name: n.online for n in cluster.nodes}
        for node in sorted(nodes.values(), key=lambda n: n.name):
            if node.name not in online:
                continue
            out.append(
                Observation(
                    level="node",
                    key=node.name,
                    name=f"node {node.name}",
                    state="healthy" if online[node.name] else "down",
                    reason=f"node {node.name} is offline in the Proxmox cluster",
                    scope={"node": node.name},
                )
            )

    # -- guests: running or not, from the root service's guest gate ----------
    seen_guests: set[str] = set()
    served = [s for s, _ in tree.walk() if tree.is_served(s)]
    for svc in served:
        if tree.parent(svc) is not None or svc.guest_kind is GuestKind.NONE or svc.guest_id is None:
            continue
        verdict = verdicts.get(svc.slug)
        details = _guest_details(verdict) if verdict else None
        if not details or details.get("node_online") is False:
            continue  # node level covers it; or the gate did not run
        status = str(details.get("guest_status") or "").lower()
        node_name = nodes[svc.node_id].name if svc.node_id in nodes else ""
        key = f"{node_name}/{svc.guest_id}"
        if key in seen_guests or not status:
            continue
        seen_guests.add(key)
        out.append(
            Observation(
                level="guest",
                key=key,
                name=f"guest {svc.guest_kind.value} {svc.guest_id} ({svc.slug})",
                state="healthy" if status == "running" else "down",
                reason=f"guest {svc.guest_kind.value} {svc.guest_id} on {node_name} is {status}",
                cause_key=None,
                scope={"node": node_name, "slug": svc.slug, "group": svc.group},
            )
        )

    # -- services, then sub-services -----------------------------------------
    for want_sub in (False, True):
        for svc in served:
            if (tree.parent(svc) is not None) is not want_sub:
                continue
            verdict = verdicts.get(svc.slug)
            if verdict is None:
                continue
            node_name = nodes[svc.node_id].name if svc.node_id in nodes else ""
            eg = tree.effective_guest(svc)
            cause: str | None = None
            if verdict.state is HealthState.DOWN:
                details = _guest_details(verdict) or {}
                if details.get("node_online") is False:
                    cause = f"node:{node_name}"
                elif eg.guest_id is not None:
                    cause = f"guest:{node_name}/{eg.guest_id}"
            out.append(
                Observation(
                    level="sub" if want_sub else "service",
                    key=svc.slug,
                    name=svc.name,
                    state=_STATE[verdict.state],
                    reason=verdict.reason,
                    cause_key=cause,
                    scope={"slug": svc.slug, "group": svc.group, "node": node_name},
                )
            )
    return out


__all__ = ["collect"]
