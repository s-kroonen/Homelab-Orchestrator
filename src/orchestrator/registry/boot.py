"""Boot order: what Proxmox starts by itself when a node boots, in which order, and how long
waking something is likely to take.

Proxmox owns the settings (a guest's *Start at boot* and *Start/Shutdown order*, a node's *Start
on boot delay*). A node that is off cannot be asked, so the last values read while it was on are
kept in :class:`GuestBootSetting` / :class:`NodeState`: :func:`refresh_node` reads them (the Nodes
& guests page and the wake pipeline call it), everything else reads the copy.

When a node boots, Proxmox waits its start-on-boot delay, then starts every ``onboot`` guest,
lowest ``order`` first (guests without an order after the ordered ones, then by VMID), and waits
each guest's ``up`` seconds before the next. A guest without ``onboot`` stays off until something
starts it. So after the orchestrator powers a node on, an ``onboot`` guest needs no start: the
wake waits for Proxmox to start it (see ``pipelines/wake.py``); any other guest is started.

Times are estimates: a node's boot time is learned from its last wakes (how long it took to come
online), a guest's from how long its service took to become healthy; defaults until there is any
history. See docs/boot_order.md.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, desc, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.proxmox.base import Guest, GuestBoot, ProxmoxAdapter
from orchestrator.db.models import GuestBootSetting, Node, NodeState, PipelineRun, Service
from orchestrator.domain.enums import PipelineKind, PipelineStatus
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Until a node has been woken a few times: how long it takes to come online.
DEFAULT_NODE_BOOT_S = 120
#: Until a service has been woken: how long from its guest starting to it answering.
DEFAULT_GUEST_BOOT_S = 45
#: What one guest start costs Proxmox during its boot sequence, before its ``up`` wait.
GUEST_START_S = 5
#: How many past wakes the estimates look at.
HISTORY = 5


# -- the copy of Proxmox's settings --------------------------------------------------------------


def settings_for(session: Session, node: str) -> dict[int, GuestBootSetting]:
    rows = session.exec(select(GuestBootSetting).where(GuestBootSetting.node == node)).all()
    return {r.vmid: r for r in rows}


def setting_for(session: Session, node: str, vmid: int) -> GuestBootSetting | None:
    return session.get(GuestBootSetting, (node, vmid))


def store(session: Session, node: str, vmid: int, boot: GuestBoot) -> GuestBootSetting:
    row = session.get(GuestBootSetting, (node, vmid)) or GuestBootSetting(node=node, vmid=vmid)
    row.onboot, row.order, row.up_s, row.down_s = boot.onboot, boot.order, boot.up_s, boot.down_s
    row.read_at = datetime.now(UTC)
    session.add(row)
    return row


async def refresh_node(
    session: Session, proxmox: ProxmoxAdapter, node: Node, guests: list[Guest] | None = None
) -> bool:
    """Read the node's start-on-boot delay and every guest's boot settings from Proxmox and keep
    them. The node must be on. Never raises: a guest that cannot be read keeps its last copy.
    Returns True when the node answered."""
    try:
        delay = await proxmox.node_onboot_delay(node.name)
        if guests is None:
            guests = await proxmox.list_guests(node.name)
    except AdapterError as exc:
        log.info("boot.refresh_skipped", node=node.name, error=str(exc))
        return False
    gate = asyncio.Semaphore(4)

    async def one(g: Guest) -> tuple[Guest, GuestBoot | None]:
        async with gate:
            try:
                return g, await proxmox.guest_boot(node.name, g.vmid, g.kind)
            except AdapterError as exc:
                log.info("boot.guest_unreadable", node=node.name, vmid=g.vmid, error=str(exc))
                return g, None

    for g, boot in await asyncio.gather(*(one(g) for g in guests if g.node == node.name)):
        if boot is not None:
            store(session, node.name, g.vmid, boot)
    if node.id is not None:
        state = session.get(NodeState, node.id) or NodeState(node_id=node.id)
        state.onboot_delay_s = delay
        state.boot_read_at = datetime.now(UTC)
        session.add(state)
    session.commit()
    return True


# -- the order Proxmox starts things in ----------------------------------------------------------


@dataclass
class BootSlot:
    """One ``onboot`` guest in a node's boot sequence."""

    vmid: int
    position: int  # 1-based
    order: int | None
    up_s: int
    #: Seconds after the node is online until Proxmox starts this guest.
    starts_after_s: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "vmid": self.vmid,
            "position": self.position,
            "order": self.order,
            "up_s": self.up_s,
            "starts_after_s": self.starts_after_s,
        }


def sequence(session: Session, node: Node) -> list[BootSlot]:
    """The ``onboot`` guests of ``node`` in the order Proxmox starts them, each with when."""
    state = session.get(NodeState, node.id) if node.id is not None else None
    at = (state.onboot_delay_s or 0) if state else 0
    rows = [r for r in settings_for(session, node.name).values() if r.onboot]
    rows.sort(key=lambda r: (r.order is None, r.order if r.order is not None else 0, r.vmid))
    out: list[BootSlot] = []
    for i, r in enumerate(rows, start=1):
        out.append(BootSlot(r.vmid, i, r.order, r.up_s or 0, at))
        at += GUEST_START_S + (r.up_s or 0)
    return out


# -- what history says ---------------------------------------------------------------------------


def _seconds(step: dict[str, Any]) -> float | None:
    try:
        a = datetime.fromisoformat(step["started_at"])
        b = datetime.fromisoformat(step["finished_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return max(0.0, (b - a).total_seconds())


def _recent_runs(session: Session, *, service_id: int | None = None) -> list[PipelineRun]:
    stmt = (
        select(PipelineRun)
        .where(PipelineRun.kind.in_([PipelineKind.WAKE, PipelineKind.BACKUP]))  # type: ignore[attr-defined]
        .where(PipelineRun.status == PipelineStatus.SUCCEEDED)
        .order_by(desc(col(PipelineRun.id)))
        .limit(200)
    )
    if service_id is not None:
        stmt = stmt.where(PipelineRun.service_id == service_id)
    return list(session.exec(stmt).all())


def node_boot_s(session: Session, node: Node) -> tuple[int, bool]:
    """(seconds, learned): how long ``node`` takes to come online after it is powered on — the
    median of its last wakes, or the default when it has none."""
    seen: list[float] = []
    for run in _recent_runs(session):
        steps = run.steps or []
        if not any(
            s.get("name") == "node_power_on" and (s.get("detail") or {}).get("node") == node.name
            for s in steps
        ):
            continue
        for s in steps:
            if s.get("name") == "node_online" and (s.get("detail") or {}).get("node") == node.name:
                took = _seconds(s)
                if took is not None and took >= 1:
                    seen.append(took)
        if len(seen) >= HISTORY:
            break
    if not seen:
        return DEFAULT_NODE_BOOT_S, False
    return int(statistics.median(seen)), True


def guest_boot_s(session: Session, service: Service) -> tuple[int, bool]:
    """(seconds, learned): from its guest starting to the service answering."""
    seen: list[float] = []
    for run in _recent_runs(session, service_id=service.id):
        for s in run.steps or []:
            if s.get("name") == "health_poll" and s.get("status") == "ok":
                took = _seconds(s)
                if took is not None:
                    seen.append(took)
        if len(seen) >= HISTORY:
            break
    if not seen:
        return DEFAULT_GUEST_BOOT_S, False
    return int(statistics.median(seen)), True


# -- one service: will it start by itself, and how long --------------------------------------------


@dataclass
class WakeEstimate:
    """What waking ``service`` involves and roughly how long it takes."""

    node: str
    node_on: bool | None  # None: not known
    always_on: bool
    vmid: int | None
    #: True: Proxmox starts the guest when the node boots; False: it has to be started;
    #: None: not known (its settings were never read).
    starts_itself: bool | None
    slot: BootSlot | None
    steps: list[dict[str, Any]] = field(default_factory=list)
    total_s: int = 0
    learned: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "node_on": self.node_on,
            "always_on": self.always_on,
            "vmid": self.vmid,
            "starts_itself": self.starts_itself,
            "slot": self.slot.as_dict() if self.slot else None,
            "steps": self.steps,
            "total_s": self.total_s,
            "learned": self.learned,
        }


def estimate(
    session: Session, service: Service, node: Node, vmid: int | None, *, node_on: bool | None
) -> WakeEstimate:
    """How waking ``service`` (on ``node``, guest ``vmid``) would go: power the node on if it is
    off, let Proxmox start the guest if it starts at boot (else start it), wait for it to answer.
    """
    slot = None
    starts_itself: bool | None = None
    if vmid is not None:
        row = setting_for(session, node.name, vmid)
        if row is not None:
            starts_itself = row.onboot
            slot = next((s for s in sequence(session, node) if s.vmid == vmid), None)
    steps: list[dict[str, Any]] = []
    learned = True
    if not node.always_on and node_on is not True:
        boot, known = node_boot_s(session, node)
        learned = learned and known
        steps.append({"what": f"power {node.name} on and wait for it", "seconds": boot})
        if vmid is not None and starts_itself and slot is not None:
            steps.append(
                {
                    "what": f"Proxmox starts it by itself ({_nth(slot.position)} in the boot order)",
                    "seconds": slot.starts_after_s + GUEST_START_S,
                }
            )
        elif vmid is not None:
            steps.append({"what": "start the guest", "seconds": GUEST_START_S})
    elif vmid is not None:
        steps.append({"what": "start the guest if it is stopped", "seconds": GUEST_START_S})
    up, known = guest_boot_s(session, service)
    learned = learned and known
    steps.append({"what": "wait until it answers", "seconds": up})
    return WakeEstimate(
        node=node.name,
        node_on=node_on,
        always_on=node.always_on,
        vmid=vmid,
        starts_itself=starts_itself,
        slot=slot,
        steps=steps,
        total_s=sum(int(s["seconds"]) for s in steps),
        learned=learned,
    )


# -- a backup: everything it wakes, in order, and how long --------------------------------------


def backup_timeline(session: Session, plan_steps: list[Any]) -> dict[str, Any]:
    """What the wake phase of a backup would do and roughly how long, from its plan's steps
    (``registry.backup_plan.PlanStep``): each step's estimate (a node powered on by an earlier
    step counts as on), the total, and the boot order of every node it powers on, with the
    guests it needs marked. A blocked step is listed but not counted. Nothing is read from
    Proxmox: the kept copy only."""
    from orchestrator.pipelines.wake import known_on, node_of
    from orchestrator.registry.tree import effective_guest

    powered: set[str] = set()
    out_steps: list[dict[str, Any]] = []
    wanted: dict[str, set[int]] = {}
    learned = True
    for step in plan_steps:
        slug = step.slug
        svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        node = node_of(session, svc) if svc is not None else None
        if svc is None or node is None:
            continue
        on = node.always_on or node.name in powered or known_on(session, node)
        if step.blocker and not on:
            out_steps.append({"slug": slug, "node": node.name, "blocked": step.blocker})
            continue
        vmid = effective_guest(session, svc).guest_id
        est = estimate(session, svc, node, vmid, node_on=on)
        learned = learned and est.learned
        if not on:
            powered.add(node.name)
        if vmid is not None:
            wanted.setdefault(node.name, set()).add(vmid)
        out_steps.append({"slug": slug, "blocked": None, **est.as_dict()})
    nodes: list[dict[str, Any]] = []
    for name in sorted(powered):
        node = session.exec(select(Node).where(Node.name == name)).one_or_none()
        if node is None:
            continue
        state = session.get(NodeState, node.id) if node.id is not None else None
        names = _guest_names(session, node)
        nodes.append(
            {
                "node": name,
                "delay_s": state.onboot_delay_s if state else None,
                "read_at": state.boot_read_at.isoformat() if state and state.boot_read_at else None,
                "sequence": [
                    {
                        **slot.as_dict(),
                        "name": names.get(slot.vmid, ""),
                        "needed": slot.vmid in wanted.get(name, set()),
                    }
                    for slot in sequence(session, node)
                ],
            }
        )
    return {
        "steps": out_steps,
        "total_s": sum(int(s.get("total_s", 0)) for s in out_steps),
        "learned": learned,
        "nodes": nodes,
    }


def _guest_names(session: Session, node: Node) -> dict[int, str]:
    """vmid -> the service that runs in it (the registry's name for it)."""
    rows = session.exec(
        select(Service)
        .where(Service.node_id == node.id)
        .where(Service.parent_id == None)  # noqa: E711
    ).all()
    return {s.guest_id: s.name or s.slug for s in rows if s.guest_id is not None}


def _nth(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


__all__ = [
    "DEFAULT_GUEST_BOOT_S",
    "DEFAULT_NODE_BOOT_S",
    "BootSlot",
    "WakeEstimate",
    "backup_timeline",
    "estimate",
    "guest_boot_s",
    "node_boot_s",
    "refresh_node",
    "sequence",
    "setting_for",
    "settings_for",
    "store",
]
