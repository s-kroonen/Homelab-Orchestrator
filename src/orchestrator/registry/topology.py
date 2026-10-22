"""The picture: what runs on which box, where traffic enters, where backups go.

Built from what the orchestrator already knows (registry + the last scan's state),
no live calls, so the map page is instant. Everything the page draws comes from
:func:`build_map`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from orchestrator.config import Settings
from orchestrator.db.models import (
    BackupPolicy,
    BackupRecord,
    Node,
    NodeState,
    Probe,
    ProxyHost,
    Service,
    ServiceState,
)
from orchestrator.domain.enums import GuestKind, HealthState
from orchestrator.ipdiscovery.discovery import effective_ip
from orchestrator.registry.host_cascade import tree_map
from orchestrator.registry.tree import TreeIndex


def _gate(state: ServiceState | None) -> dict[str, Any]:
    results = state.last_probe_results if state else []
    first = results[0] if results else None
    if first and first.get("kind") == "guest":
        return dict(first.get("details") or {})
    return {}


def _verdict(svc: Service, served: bool, state: ServiceState | None) -> str:
    if not svc.enabled:
        return "disabled"
    if not served:
        return "parked"  # an ancestor is disabled
    return state.last_verdict.value if state else HealthState.UNKNOWN.value


def _service_node(
    svc: Service,
    tree: TreeIndex,
    states: dict[int, ServiceState],
    probes: dict[int, list[Probe]],
    hosts: dict[int, list[ProxyHost]],
    host_served: dict[int, bool],
    records: dict[int, BackupRecord],
    policies: dict[int, str],
) -> dict[str, Any]:
    assert svc.id is not None
    state = states.get(svc.id)
    ps = [p for p in probes.get(svc.id, []) if p.enabled]
    ip, _src = effective_ip(svc, state)
    record = records.get(state.last_backup_id) if state and state.last_backup_id else None
    out: dict[str, Any] = {
        "id": f"svc:{svc.slug}",
        "slug": svc.slug,
        "name": svc.name,
        "kind": "sub" if svc.parent_id is not None else "service",
        "enabled": svc.enabled,
        "served": tree.is_served(svc),
        "verdict": _verdict(svc, tree.is_served(svc), state),
        "reason": (state.notes or "") if state else "",
        "group": svc.group,
        "ip": ip,
        "controller": str((svc.controller or {}).get("type", "")),
        "recovery": bool(svc.recovery) and bool(svc.recovery.get("enabled", True)),
        "backup_gate": svc.backup_gate.value if svc.parent_id is not None else "",
        "probes": {
            "total": len(ps),
            "required": sum(1 for p in ps if p.required),
        },
        "hosts": [
            {
                "hostname": h.hostname,
                "enabled": h.enabled,
                "served": bool(host_served.get(h.id or -1, h.enabled)),
            }
            for h in hosts.get(svc.id, [])
        ],
        "policy": policies.get(svc.backup_policy_id or -1, ""),
        "last_backup": (
            {
                "at": (
                    record.created_at.replace(tzinfo=UTC)
                    if record.created_at.tzinfo is None
                    else record.created_at
                ).isoformat(),
                "verified": record.verified,
            }
            if record
            else None
        ),
        "children": [
            _service_node(c, tree, states, probes, hosts, host_served, records, policies)
            for c in tree.kids(svc)
        ],
    }
    return out


def build_map(session: Session, settings: Settings) -> dict[str, Any]:
    tree = TreeIndex.build(session)
    nodes = list(session.exec(select(Node).order_by(Node.name)).all())
    node_states = {n.node_id: n for n in session.exec(select(NodeState)).all()}
    states = {s.service_id: s for s in session.exec(select(ServiceState)).all()}
    policies = {p.id: p.name for p in session.exec(select(BackupPolicy)).all() if p.id is not None}
    policy_rows = list(session.exec(select(BackupPolicy)).all())
    records = {r.id: r for r in session.exec(select(BackupRecord)).all() if r.id is not None}
    probes: dict[int, list[Probe]] = {}
    for p in session.exec(select(Probe)).all():
        probes.setdefault(p.service_id, []).append(p)
    hosts: dict[int, list[ProxyHost]] = {}
    all_hosts = list(session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all())
    for h in all_hosts:
        if h.service_id is not None:
            hosts.setdefault(h.service_id, []).append(h)
    tree_ok = tree_map(session)
    host_served = {
        hid: all_hosts_enabled and tree_ok.get(hid, True)
        for hid, all_hosts_enabled in ((h.id, h.enabled) for h in all_hosts if h.id is not None)
    }

    def build(svc: Service) -> dict[str, Any]:
        return _service_node(svc, tree, states, probes, hosts, host_served, records, policies)

    roots = [s for s, _ in tree.walk() if tree.parent(s) is None]
    node_out: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for node in nodes:
        mine = [r for r in roots if r.node_id == node.id]
        node_gate: dict[str, Any] = {}
        for r in mine:
            g = _gate(states.get(r.id) if r.id is not None else None)
            if g:
                node_gate = g
                break
        ns = node_states.get(node.id) if node.id is not None else None
        if node_gate.get("node_online") is True:
            power = "on"
        elif node_gate.get("node_online") is False:
            power = "off"
        else:
            power = ns.power_state.value if ns else "unknown"
        guests: dict[str, dict[str, Any]] = {}
        loose: list[dict[str, Any]] = []
        for r in mine:
            sv = build(r)
            if r.guest_kind is GuestKind.NONE or r.guest_id is None:
                loose.append(sv)
                continue
            key = f"{node.name}/{r.guest_id}"
            gate = _gate(states.get(r.id) if r.id is not None else None)
            g = guests.setdefault(
                key,
                {
                    "id": f"guest:{key}",
                    "key": key,
                    "kind": r.guest_kind.value,
                    "vmid": r.guest_id,
                    "status": str(gate.get("guest_status") or "unknown").lower(),
                    "services": [],
                },
            )
            g["services"].append(sv)
        node_out.append(
            {
                "id": f"node:{node.name}",
                "name": node.name,
                "power": power,
                "address": node.address,
                "always_on": node.always_on,
                "power_target": node.power_mgr_target,
                "guests": sorted(guests.values(), key=lambda g: g["vmid"]),
                "loose": loose,
            }
        )

    # -- edges ------------------------------------------------------------------
    for svc in tree.by_slug.values():
        for dep in svc.depends_on or []:
            if dep in tree.by_slug:
                edges.append(
                    {
                        "kind": "depends",
                        "from": f"svc:{svc.slug}",
                        "to": f"svc:{dep}",
                        "label": "depends on",
                    }
                )
    entry_hosts = []
    for h in all_hosts:
        svc = tree.by_id.get(h.service_id) if h.service_id is not None else None
        entry_hosts.append(
            {
                "id": f"host:{h.hostname}",
                "hostname": h.hostname,
                "enabled": h.enabled,
                "served": bool(host_served.get(h.id or -1, h.enabled)),
                "service": svc.slug if svc else None,
                "provider": h.router_provider.value,
            }
        )
        if svc is not None:
            edges.append(
                {
                    "kind": "proxy",
                    "from": f"host:{h.hostname}",
                    "to": f"svc:{svc.slug}",
                    "off": not host_served.get(h.id or -1, h.enabled),
                }
            )

    backup_targets = []
    for svc in roots:
        if svc.backup_excluded or svc.guest_kind is GuestKind.NONE:
            continue
        pol = policies.get(svc.backup_policy_id or -1)
        state = states.get(svc.id) if svc.id is not None else None
        rec = records.get(state.last_backup_id) if state and state.last_backup_id else None
        backup_targets.append(
            {
                "slug": svc.slug,
                "policy": pol or "",
                "last_backup": rec.created_at.isoformat() if rec else None,
                "verified": bool(rec and rec.verified),
            }
        )
        if pol:
            edges.append(
                {
                    "kind": "backup",
                    "from": f"svc:{svc.slug}",
                    "to": "pbs",
                    "label": pol,
                    "stale": rec is None,
                }
            )
            for sub in tree.descendants(svc):
                if sub.backup_gate.value == "required" and tree.is_served(sub):
                    edges.append(
                        {
                            "kind": "waits",
                            "from": f"svc:{svc.slug}",
                            "to": f"svc:{sub.slug}",
                            "label": "backup waits for",
                        }
                    )
    for pol in policy_rows:
        for slug in pol.requires or []:
            if slug in tree.by_slug:
                edges.append(
                    {
                        "kind": "requires",
                        "from": "pbs",
                        "to": f"svc:{slug}",
                        "label": f"{pol.name}: needs awake",
                    }
                )
    for svc in roots:
        for slug in svc.backup_requires or []:
            if slug in tree.by_slug:
                edges.append(
                    {
                        "kind": "requires",
                        "from": "pbs",
                        "to": f"svc:{slug}",
                        "label": f"{svc.slug}: needs awake",
                    }
                )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "nodes": node_out,
        "entry": {"id": "entry", "name": "Traefik", "hosts": entry_hosts},
        "backup": {
            "id": "pbs",
            "datastore": settings.pbs_datastore,
            "storage": settings.pve_backup_storage,
            "targets": backup_targets,
        },
        "edges": edges,
        "counts": {
            "nodes": len(node_out),
            "services": len(roots),
            "sub_services": len(tree.by_id) - len(roots),
            "hosts": len(entry_hosts),
        },
    }
