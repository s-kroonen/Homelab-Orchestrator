"""Read Proxmox guests and keep the result: what the health cycle and *Read now* call.

The one place that wires the Proxmox provider to the collector, the store and the checks.
Proxmox is read with no database transaction open; the result is written after.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.proxmox.base import Guest
from orchestrator.config import Settings
from orchestrator.db.models import Node
from orchestrator.db.session import session_scope
from orchestrator.guests import checks, store
from orchestrator.guests.collector import GuestCollector
from orchestrator.guests.model import GuestFacts, GuestRef, GuestTarget
from orchestrator.guests.providers.proxmox import ProxmoxGuestProvider
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


def guest_key(node: str, vmid: int) -> str:
    return f"{node}/{vmid}"


def proxmox_targets(session: Session, guests: list[Guest]) -> list[GuestTarget]:
    """Proxmox's guest list as targets, each under the source that owns its node."""
    source_of = {n.name: n.source_id for n in session.exec(select(Node)).all()}
    return [
        GuestTarget(
            ref=GuestRef(source_of.get(g.node, ""), guest_key(g.node, g.vmid)),
            name=g.name,
            running=g.status == "running",
            kind=g.kind.value,
            node=g.node,
            vmid=g.vmid,
        )
        for g in guests
    ]


def collector_for(proxmox: Any, settings: Settings) -> GuestCollector:
    return GuestCollector(ProxmoxGuestProvider(proxmox, timeout_s=settings.agent_timeout_s))


async def collect(
    proxmox: Any, settings: Settings, guests: list[Guest], notifications: Any = None
) -> list[GuestFacts]:
    """Read every guest in ``guests``, keep the readings, announce what changed."""
    with session_scope() as session:
        targets = proxmox_targets(session, guests)
        previous = store.load_all(session)
    readings = await collector_for(proxmox, settings).collect(targets, previous)
    with session_scope() as session:
        store.save_many(session, readings)
        for source in {t.ref.source for t in targets}:
            store.forget_missing(
                session, source, {t.ref.key for t in targets if t.ref.source == source}
            )
        session.commit()
        if notifications is not None:
            await notifications.observe(session, checks.observations(session, readings))
    log.info(
        "guests.collected",
        guests=len(readings),
        answering=sum(1 for r in readings if r.agent.value == "answers"),
    )
    return readings


async def read_now(proxmox: Any, settings: Settings, guest: Guest) -> GuestFacts:
    """Read one guest at once (back-off or not) and keep it. Alerts follow on the next cycle,
    like any other change, so one press cannot page anybody."""
    with session_scope() as session:
        target = proxmox_targets(session, [guest])[0]
        previous = store.load(session, target.ref)
    facts = await collector_for(proxmox, settings).read_one(target, previous)
    with session_scope() as session:
        store.save(session, facts)
        session.commit()
    return facts


__all__ = ["collect", "collector_for", "guest_key", "proxmox_targets", "read_now"]
