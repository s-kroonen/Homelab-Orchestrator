"""How full the guests' disks are, and an alert before one runs out.

Every health cycle (scheduler/health_scanner.py) reads, for each running guest:

* a container (LXC): its root filesystem, which Proxmox measures itself and lists with the
  cluster's resources - no extra call;
* a VM: every filesystem mounted inside it, from the QEMU guest agent (``get-fsinfo``). A VM
  without the agent is *not measured*: shown as such, never alerted.

The fullest filesystem decides: at ``DISK_WARN_PCT`` (85) it is ``low`` - a warning, event
``disk_low`` - and at ``DISK_FULL_PCT`` (95) ``full`` - an error, ``disk_full``. Going back
needs a few points of room (``_HYSTERESIS``), so a disk that hovers around a threshold does not
alert on every cycle. The alerts go through the notification engine like any other (level
``disk``, debounced, rule-routed); a guest that is not running is left as it was.

The last result is kept in the ``disks.status`` setting for the Nodes & guests page and the
``disk_usage`` assistant tool.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.proxmox.base import Filesystem, Guest, GuestFilesystems
from orchestrator.config import Settings
from orchestrator.db.models import Node, Service, Setting, TargetState
from orchestrator.domain.enums import GuestKind
from orchestrator.logging_setup import get_logger
from orchestrator.notify.engine import Observation

log = get_logger(__name__)

SETTING_KEY = "disks.status"
LEVEL = "disk"
#: Points below the warning threshold a disk must drop to before it counts as fine again.
_HYSTERESIS = 3.0
_CONCURRENCY = 4
_PER_GUEST_TIMEOUT_S = 10.0

#: state -> the notification engine's state for it.
_OBSERVED = {"ok": "healthy", "low": "disk_low", "full": "disk_full"}
_FROM_OBSERVED = {v: k for k, v in _OBSERVED.items()}


@dataclass
class GuestDisks:
    node: str
    vmid: int
    kind: str  # vm | lxc
    name: str
    status: str  # the guest's own: running | stopped | ...
    #: ok | low | full | unmeasured (running, but nothing could be read) | off (not running)
    state: str
    filesystems: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None
    error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.node}/{self.vmid}"

    @property
    def worst(self) -> dict[str, Any] | None:
        return max(self.filesystems, key=lambda f: f["percent"], default=None)


@dataclass
class DiskStatus:
    guests: list[GuestDisks] = field(default_factory=list)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    warn_pct: int = 85
    full_pct: int = 95

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "warn_pct": self.warn_pct,
            "full_pct": self.full_pct,
            "guests": [{**asdict(g), "worst": g.worst} for g in self.guests],
        }


def _fs(f: Filesystem) -> dict[str, Any]:
    return {
        "mount": f.mount,
        "fstype": f.fstype,
        "used": f.used,
        "total": f.total,
        "percent": f.percent,
    }


def classify(percent: float, settings: Settings, previous: str | None = None) -> str:
    """``ok``, ``low`` or ``full`` for the fullest filesystem; ``previous`` (the state it was
    announced in) keeps a disk that is just under a threshold where it was."""
    warn, full = float(settings.disk_warn_pct), float(settings.disk_full_pct)
    if percent >= full:
        return "full"
    if previous == "full" and percent >= full - _HYSTERESIS:
        return "full"
    if percent >= warn:
        return "low"
    if previous in ("low", "full") and percent >= warn - _HYSTERESIS:
        return "low"
    return "ok"


async def _read(proxmox: Any, guest: Guest, sem: asyncio.Semaphore) -> GuestFilesystems:
    if guest.kind is GuestKind.CT and guest.maxdisk > 0:
        # Proxmox measures a container's root filesystem itself: it came with the list.
        from orchestrator.adapters.proxmox.disks import lxc_root

        return GuestFilesystems(filesystems=lxc_root(guest.disk, guest.maxdisk), source="lxc")
    async with sem:
        try:
            result: GuestFilesystems = await asyncio.wait_for(
                proxmox.guest_filesystems(guest.node, guest.vmid, guest.kind),
                _PER_GUEST_TIMEOUT_S,
            )
            return result
        except TimeoutError:
            return GuestFilesystems(error=f"no answer within {_PER_GUEST_TIMEOUT_S:g}s")
        except Exception as exc:  # the adapter should not raise; be safe anyway
            return GuestFilesystems(error=f"{type(exc).__name__}: {exc}")


async def check_disks(
    proxmox: Any,
    settings: Settings,
    *,
    previous: dict[str, str] | None = None,
    guests: list[Guest] | None = None,
) -> DiskStatus:
    """Read every guest. ``guests``: the list the health cycle already read (so it is not read
    twice). Raises only when Proxmox cannot list the guests at all."""
    previous = previous or {}
    if guests is None:
        guests = list(await proxmox.list_guests())
    sem = asyncio.Semaphore(_CONCURRENCY)
    running = [g for g in guests if g.status == "running"]
    reads = await asyncio.gather(*(_read(proxmox, g, sem) for g in running))
    found = {(g.node, g.vmid): r for g, r in zip(running, reads, strict=True)}
    out: list[GuestDisks] = []
    for g in guests:
        kind = "lxc" if g.kind is GuestKind.CT else "vm"
        read = found.get((g.node, g.vmid))
        if read is None:
            out.append(GuestDisks(g.node, g.vmid, kind, g.name, g.status, "off"))
            continue
        filesystems = [_fs(f) for f in read.filesystems]
        if not filesystems:
            out.append(
                GuestDisks(g.node, g.vmid, kind, g.name, g.status, "unmeasured", error=read.error)
            )
            continue
        worst = max(f["percent"] for f in filesystems)
        state = classify(worst, settings, previous.get(f"{g.node}/{g.vmid}"))
        out.append(
            GuestDisks(g.node, g.vmid, kind, g.name, g.status, state, filesystems, read.source)
        )
    return DiskStatus(guests=out, warn_pct=settings.disk_warn_pct, full_pct=settings.disk_full_pct)


def previous_states(session: Session) -> dict[str, str]:
    """What each guest's disks were last announced as (``node/vmid`` -> ok | low | full)."""
    rows = session.exec(select(TargetState).where(TargetState.level == LEVEL)).all()
    return {r.key: _FROM_OBSERVED[r.state] for r in rows if r.state in _FROM_OBSERVED}


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"  # pragma: no cover


def describe(g: GuestDisks) -> str:
    """``/ is 91% full (9.1 GB of 10.0 GB used); /data 40%``"""
    parts: list[str] = []
    for f in sorted(g.filesystems, key=lambda f: -f["percent"]):
        if not parts:
            parts.append(
                f"{f['mount']} is {f['percent']:g}% full "
                f"({_size(f['used'])} of {_size(f['total'])} used)"
            )
        else:
            parts.append(f"{f['mount']} {f['percent']:g}%")
    return "; ".join(parts)


def observations(session: Session, status: DiskStatus) -> list[Observation]:
    """What the notification engine is told: one per measured, running guest."""
    nodes = {n.id: n.name for n in session.exec(select(Node)).all()}
    services = {
        (nodes.get(s.node_id, ""), s.guest_id): s
        for s in session.exec(select(Service).where(Service.guest_id != None)).all()  # noqa: E711
        if s.guest_kind is not GuestKind.NONE
    }
    out: list[Observation] = []
    for g in status.guests:
        if g.state not in _OBSERVED:
            continue  # not running, or nothing to measure: leave it as it was
        svc = services.get((g.node, g.vmid))
        worst = g.worst or {}
        name = f"Disk on {g.name or g.vmid}"
        title = None
        if g.state in ("low", "full"):
            title = f"{name} is {worst.get('percent', 0):g}% full ({worst.get('mount', '?')})"
        limit = status.full_pct if g.state == "full" else status.warn_pct
        reason = describe(g) + (
            f". The {'error' if g.state == 'full' else 'warning'} threshold is {limit}%."
            if g.state in ("low", "full")
            else ""
        )
        out.append(
            Observation(
                level=LEVEL,
                key=g.key,
                name=name,
                state=_OBSERVED[g.state],
                reason=reason,
                scope={
                    "node": g.node,
                    "slug": svc.slug if svc else "",
                    "group": svc.group if svc else "",
                },
                severity={"low": "warn", "full": "error"}.get(g.state),
                title=title,
            )
        )
    return out


def save(session: Session, status: DiskStatus) -> None:
    row = session.get(Setting, SETTING_KEY)
    if row is None:
        session.add(Setting(key=SETTING_KEY, value=status.as_dict()))
    else:
        row.value = status.as_dict()
        row.updated_at = datetime.now(UTC)
        session.add(row)


def load(session: Session) -> dict[str, Any] | None:
    row = session.get(Setting, SETTING_KEY)
    return dict(row.value) if row is not None and row.value else None


def by_guest(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """``node/vmid`` -> that guest's entry in a stored snapshot (with the check's time)."""
    if not snapshot:
        return {}
    at = snapshot.get("at")
    return {f"{g['node']}/{g['vmid']}": {**g, "at": at} for g in snapshot.get("guests", [])}


__all__ = [
    "DiskStatus",
    "GuestDisks",
    "by_guest",
    "check_disks",
    "classify",
    "describe",
    "load",
    "observations",
    "previous_states",
    "save",
]
