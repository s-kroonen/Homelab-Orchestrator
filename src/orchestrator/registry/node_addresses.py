"""Fill in a node's address from what the Proxmox cluster reports.

A node's address is what the orchestrator falls back to when the configured Proxmox host
does not answer, and what it judges the node by when no Proxmox API answers at all
(health/guest_gate.py). So every health cycle that reads the cluster fills in the ones
still blank; the Nodes page's "Fill addresses" does the same on demand. An address that
was set by hand is never changed.
"""

from __future__ import annotations

from sqlmodel import Session, select

from orchestrator.adapters.proxmox.base import ClusterStatus
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import Node


def fill_from_cluster(
    session: Session, cluster: ClusterStatus, settings: Settings, *, actor: str
) -> tuple[dict[str, str], list[str]]:
    """``(filled {node: address}, skipped [node])``. Audited; the caller commits."""
    by_name = {n.name: n for n in cluster.nodes}
    filled: dict[str, str] = {}
    skipped: list[str] = []
    if cluster.synthetic:
        return filled, skipped  # judged by the addresses themselves: nothing new to learn
    for node in session.exec(select(Node).order_by(Node.name)).all():
        if node.address.strip():
            continue
        entry = by_name.get(node.name)
        address = (settings.proxmox_host if entry.local else entry.ip) if entry else ""
        if not address:
            skipped.append(node.name)
            continue
        node.address = address
        session.add(node)
        filled[node.name] = address
    if filled:
        audit.record(
            session,
            actor=actor,
            action="node.addresses_filled",
            target="nodes",
            details={"filled": filled},
        )
    return filled, skipped


__all__ = ["fill_from_cluster"]
