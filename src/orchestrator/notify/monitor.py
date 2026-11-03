"""Turn one health-scan cycle into observations for the notification engine.

Cause first, so the engine can fold effects under what explains them:

    network   the orchestrator's own LAN, internet and proxy checks (health/network.py)
    node      what the Proxmox cluster says; with no API answering, whether the node's
              own address answers; with neither, unknown
    guest     running or not
    service   and sub-service verdicts, grouped under their node, guest, dependency or
              the network check that explains them
    route     each proxy host: inside through the proxy, and from outside
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session, select

from orchestrator.db.models import Node
from orchestrator.domain.enums import GuestKind, HealthState
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.health.network import NetworkStatus
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


def _network(network: NetworkStatus | None) -> list[Observation]:
    out: list[Observation] = []
    if network is None:
        return out
    lan_down = network.lan_ok is False
    for check in network.checks:
        if check.ok is None:
            continue  # not configured
        out.append(
            Observation(
                level="network",
                key=check.name,
                name=check.label,
                state="healthy" if check.ok else "down",
                reason=f"the orchestrator cannot reach {check.label} ({check.target}): "
                f"{check.message}",
                cause_key="network:lan" if lan_down and check.name != "lan" else None,
                scope={},
            )
        )
    return out


def _nodes(
    nodes: dict[Any, Node], cluster: Any | None, network: NetworkStatus | None
) -> list[Observation]:
    out: list[Observation] = []
    lan_cause = "network:lan" if network is not None and network.lan_ok is False else None
    online = {n.name: n for n in cluster.nodes} if cluster is not None else {}
    for node in sorted(nodes.values(), key=lambda n: n.name):
        entry = online.get(node.name)
        if cluster is not None and entry is None:
            continue  # not in this cluster
        if entry is None:
            state = "unknown"
            reason = (
                f"node {node.name}: no Proxmox API answers and it has no address to check, "
                "so its state cannot be determined"
            )
        elif entry.online:
            state, reason = "healthy", f"node {node.name} is online"
        elif getattr(cluster, "synthetic", False):
            state = "down"
            reason = (
                f"node {node.name} does not answer at {entry.ip or node.address} and no "
                "Proxmox API answers: it is offline or off the network"
            )
        else:
            state, reason = "down", f"node {node.name} is offline in the Proxmox cluster"
        out.append(
            Observation(
                level="node",
                key=node.name,
                name=f"node {node.name}",
                state=state,
                reason=reason,
                cause_key=lan_cause,
                scope={"node": node.name},
            )
        )
    return out


_ROUTE_RANK = {"down": 0, "failed": 1, "unknown": 2, "healthy": 3}
_ROUTE_WAY = {"traefik:": "inside, through the proxy", "public:": "from outside"}


def _routes(
    svc: Any,
    verdict: ServiceVerdict,
    network: NetworkStatus | None,
    service_key: str,
) -> list[Observation]:
    """One observation per proxy host of this service, from its route probes."""
    by_host: dict[str, list[tuple[str, Any]]] = {}
    for r in verdict.probe_results:
        for prefix in _ROUTE_WAY:
            if r.probe_name.startswith(prefix) and not r.details.get("skipped"):
                by_host.setdefault(r.probe_name[len(prefix) :], []).append((prefix, r))
    out: list[Observation] = []
    for host, results in sorted(by_host.items()):
        seen: list[tuple[str, str, Any]] = []
        for prefix, r in results:
            if r.state is HealthState.HEALTHY:
                state = "healthy"
            elif r.state is HealthState.FAILED:
                state = "failed"
            elif r.details.get("unreachable"):
                state = "down"
            else:
                state = "unknown"
            seen.append((state, prefix, r))
        worst = min(seen, key=lambda x: _ROUTE_RANK[x[0]])
        state = worst[0]
        bad = [f"{_ROUTE_WAY[p]}: {r.message}" for s, p, r in seen if s != "healthy"]
        cause: str | None = None
        if verdict.state is not HealthState.HEALTHY:
            cause = service_key  # the service behind it is the problem
        elif network is not None and network.proxy_ok is False:
            cause = "network:proxy"
        out.append(
            Observation(
                level="route",
                key=host,
                name=f"route {host}",
                state=state,
                reason="; ".join(bad) if bad else f"{host} answers",
                cause_key=cause,
                scope={"slug": svc.slug, "group": svc.group},
            )
        )
    return out


def collect(
    session: Session,
    verdicts: Mapping[str, ServiceVerdict],
    cluster: Any | None,
    tree: TreeIndex,
    network: NetworkStatus | None = None,
) -> list[Observation]:
    """Observations in cause-first order: network, nodes, guests, root services,
    sub-services, routes."""
    nodes = {n.id: n for n in session.exec(select(Node)).all()}
    out: list[Observation] = [*_network(network), *_nodes(nodes, cluster, network)]

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

    # -- services, then sub-services, then their routes -------------------------
    routes: list[Observation] = []
    for want_sub in (False, True):
        for svc in served:
            if (tree.parent(svc) is not None) is not want_sub:
                continue
            verdict = verdicts.get(svc.slug)
            if verdict is None:
                continue
            node_name = nodes[svc.node_id].name if svc.node_id in nodes else ""
            eg = tree.effective_guest(svc)
            cause = _cause_key(verdict.cause, tree)
            details = _guest_details(verdict) or {}
            if details.get("node_online") is False:
                cause = f"node:{details.get('node') or node_name}"
            elif (
                verdict.state is HealthState.DOWN
                and cause is None
                and eg.guest_id is not None
                and not verdict.unreachable
            ):
                cause = f"guest:{node_name}/{eg.guest_id}"
            level = "sub" if want_sub else "service"
            out.append(
                Observation(
                    level=level,
                    key=svc.slug,
                    name=svc.name,
                    state=_STATE[verdict.state],
                    reason=verdict.reason,
                    cause_key=cause,
                    scope={"slug": svc.slug, "group": svc.group, "node": node_name},
                    severity="error" if verdict.unreachable else None,
                )
            )
            routes.extend(_routes(svc, verdict, network, f"{level}:{svc.slug}"))
    return [*out, *routes]


def _cause_key(cause: str | None, tree: TreeIndex) -> str | None:
    """A verdict's cause as an observation key: ``service:<slug>`` becomes ``sub:<slug>``
    when that service is a sub-service."""
    if not cause or not cause.startswith("service:"):
        return cause
    slug = cause.split(":", 1)[1]
    dep = next((s for s in tree.services if s.slug == slug), None)
    if dep is not None and tree.parent(dep) is not None:
        return f"sub:{slug}"
    return cause


__all__ = ["collect"]
