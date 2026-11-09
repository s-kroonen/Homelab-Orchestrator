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


__all__ = ["OWNERS", "claim_now", "claim_unowned", "owned_by"]
