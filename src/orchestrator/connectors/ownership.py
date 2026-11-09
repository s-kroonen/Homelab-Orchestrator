"""Which source a node or proxy host belongs to (``source_id``, OR-77).

Rows from before sources had owners, and rows made without naming one, are *claimed* by the
default source of their kind: nodes by the default Proxmox, proxy hosts by the default proxy.
That runs at start and whenever connections change, so it is never stale for long, and it never
moves a row that already has an owner.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from orchestrator.connections import runtime
from orchestrator.db.models import Node, ProxyHost
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Table -> the kind of source that owns its rows.
OWNERS = ((Node, "proxmox"), (ProxyHost, "proxy"))


def claim_unowned(session: Session) -> dict[str, int]:
    """Give every unowned node and proxy host to the default source of its kind. Returns how
    many rows each kind claimed. Kinds with no source leave their rows unowned."""
    snap = runtime.current()
    claimed: dict[str, int] = {}
    for model, kind in OWNERS:
        owner = snap.primary(kind)
        if owner is None:
            continue
        rows = session.exec(select(model).where(model.source_id == "")).all()
        for row in rows:
            row.source_id = owner.id
            session.add(row)
        if rows:
            claimed[kind] = len(rows)
    if claimed:
        session.commit()
        log.info("sources.claimed", **claimed)
    return claimed


def claim_now() -> None:
    """:func:`claim_unowned` in its own session; never raises (a failure is logged and the
    next start or connection change tries again)."""
    from orchestrator.db.session import session_scope

    try:
        with session_scope() as session:
            claim_unowned(session)
    except Exception as exc:
        log.warning("sources.claim_failed", error=str(exc))


def owned_by(session: Session, model: Any, source_id: str, *, default: bool) -> list[Any]:
    """The rows of ``model`` that belong to ``source_id``; for the default source of a kind
    also the unowned ones (they are its until claimed)."""
    col = model.source_id
    query: Any = select(model).where(col.in_([source_id, ""]) if default else col == source_id)
    return list(session.exec(query).all())


def nodes_of(session: Session, source_id: str) -> list[Node]:
    """The nodes of one Proxmox source (the default one also has the unowned ones)."""
    from orchestrator.connectors.registry import default_source

    found = default_source("proxmox")
    is_default = found is not None and found.id == source_id
    return sorted(owned_by(session, Node, source_id, default=is_default), key=lambda n: n.name)


def node_href(node: Node | None, *, tab: str = "nodes", find: str = "") -> str:
    """The page that shows a node (``tab="guests"``: its guests, ``find`` filtering them): its
    Proxmox source's page. An unowned node is the default source's; with no Proxmox source at
    all, All sources."""
    from urllib.parse import quote, urlencode

    from orchestrator.connectors.registry import default_source

    owner = node.source_id if node is not None else ""
    if not owner:
        found = default_source("proxmox")
        owner = found.id if found is not None else ""
    if not owner:
        return "/sources"
    query = {"tab": tab} | ({"q": find} if find else {})
    return f"/sources/{quote(owner, safe='')}?{urlencode(query)}"


def node_href_by_name(name: str, *, tab: str = "nodes", find: str = "") -> str:
    """:func:`node_href` for a node known only by name (an alert's target)."""
    from orchestrator.db.session import get_engine

    try:
        with Session(get_engine()) as session:
            node = session.exec(select(Node).where(Node.name == name)).first()
    except Exception:  # no database to ask (a link is never worth failing an alert for)
        node = None
    return node_href(node, tab=tab, find=find)


def proxmox_of(adapters: Any, source_id: str) -> Any:
    """The Proxmox adapter of one source; the default one when it names none (or none that
    has an adapter)."""
    found = adapters.for_source(source_id) if source_id else None
    return found if found is not None else adapters.proxmox


def proxmox_for_node(session: Session, adapters: Any, node_name: str) -> Any:
    """The Proxmox adapter of the cluster a node belongs to: guest actions on that node go
    there, not to the default cluster."""
    node = session.exec(select(Node).where(Node.name == node_name)).first()
    return proxmox_of(adapters, node.source_id if node is not None else "")


__all__ = [
    "OWNERS",
    "claim_now",
    "claim_unowned",
    "node_href",
    "node_href_by_name",
    "nodes_of",
    "owned_by",
    "proxmox_for_node",
    "proxmox_of",
]
