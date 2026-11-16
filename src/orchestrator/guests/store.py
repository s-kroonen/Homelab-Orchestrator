"""The latest :class:`GuestFacts` of each guest, in the ``guest_facts`` table.

Only the newest reading is kept: history (a sign-in, an alert) goes to the notification feed
and Activity like everything else. The caller commits.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlmodel import Session, select

from orchestrator.db.models import GuestFactsRow
from orchestrator.guests.model import GuestFacts, GuestRef, parse_time


def load(session: Session, ref: GuestRef) -> GuestFacts | None:
    row = session.get(GuestFactsRow, (ref.source, ref.key))
    return GuestFacts.from_dict(row.facts) if row is not None and row.facts else None


def load_by_key(session: Session, key: str) -> GuestFacts | None:
    """A guest's reading by its key alone, whichever source lists it."""
    row = session.exec(select(GuestFactsRow).where(GuestFactsRow.guest_key == key)).first()
    return GuestFacts.from_dict(row.facts) if row is not None and row.facts else None


def load_all(session: Session, source: str | None = None) -> dict[GuestRef, GuestFacts]:
    """Every stored reading (``source``: only that source's)."""
    stmt = select(GuestFactsRow)
    if source is not None:
        stmt = stmt.where(GuestFactsRow.source_id == source)
    out: dict[GuestRef, GuestFacts] = {}
    for row in session.exec(stmt).all():
        if row.facts:
            out[GuestRef(row.source_id, row.guest_key)] = GuestFacts.from_dict(row.facts)
    return out


def by_key(session: Session) -> dict[str, GuestFacts]:
    """Every stored reading by guest key alone (Proxmox ``node/vmid`` is unique across the
    clusters a homelab runs: node names are)."""
    return {ref.key: facts for ref, facts in load_all(session).items()}


def save(session: Session, facts: GuestFacts) -> None:
    save_many(session, [facts])


def save_many(session: Session, readings: Iterable[GuestFacts]) -> None:
    now = datetime.now(UTC)
    for facts in readings:
        row = session.get(GuestFactsRow, (facts.ref.source, facts.ref.key))
        if row is None:
            row = GuestFactsRow(source_id=facts.ref.source, guest_key=facts.ref.key)
        row.facts = facts.as_dict()
        row.read_at = parse_time(facts.read_at)
        row.updated_at = now
        session.add(row)


def forget_missing(session: Session, source: str, keep: set[str]) -> int:
    """Drop the readings of guests a source no longer lists (deleted VMs). Returns how many."""
    gone = [
        row
        for row in session.exec(select(GuestFactsRow).where(GuestFactsRow.source_id == source))
        if row.guest_key not in keep
    ]
    for row in gone:
        session.delete(row)
    return len(gone)


__all__ = ["by_key", "forget_missing", "load", "load_all", "load_by_key", "save", "save_many"]
