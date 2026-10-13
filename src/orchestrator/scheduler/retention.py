"""Retention enforcement for one service under one :class:`BackupPolicy`.

Plain async function, no APScheduler in sight, so it is unit-testable with a
fake PBS adapter. The scheduler calls it after every *successful* scheduled
backup; a failed run never prunes (the last known-good backup is exactly what
a failed run must not put at risk).

Order matters:

1. **Pin first, prune second.** If ``protect_latest_verified`` is on, the
   newest verified snapshot is protected in PBS *before* prune runs. PBS's own
   prune never removes a protected snapshot, so the pin is enforced by PBS
   itself rather than by us filtering a list and hoping.
2. **The pin moves forward.** Older snapshots that *we* pinned (recorded as
   ``BackupRecord.protected``) are unpinned once a newer verified one is
   pinned. Pins we did not set — e.g. an operator protecting a snapshot by
   hand in the PBS UI — are never touched: we only unpin what the DB says we
   pinned.
3. **Prune delegates the bucketing to PBS.** ``keep_daily``/``keep_monthly``
   (and ``keep_last``) are passed straight to PBS's prune, which already
   implements the calendar-bucket semantics; reimplementing them here would
   only create a second, subtly different definition of "daily".

DRY_RUN is respected twice over: the adapter factory already hands us a
log-only PBS adapter when ``DRY_RUN=true``, and ``dry_run=settings.dry_run``
is also passed to prune so a live adapter can never be made to delete
anything while the flag is on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.pbs.base import PbsAdapter, Snapshot
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import BackupPolicy, BackupRecord, Service
from orchestrator.domain.enums import GuestKind
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Retention keys a policy may set. Mirrors what :meth:`PbsAdapter.prune` accepts.
COUNT_KEYS: tuple[str, ...] = ("keep_last", "keep_daily", "keep_monthly")
FLAG_KEYS: tuple[str, ...] = ("protect_latest_verified",)
RETENTION_KEYS: frozenset[str] = frozenset(COUNT_KEYS + FLAG_KEYS)

_KIND_TO_PBS_TYPE: dict[GuestKind, str] = {GuestKind.VM: "vm", GuestKind.CT: "ct"}


class RetentionError(Exception):
    """The policy or service cannot be mapped onto a PBS prune."""


@dataclass
class RetentionResult:
    slug: str
    group: str = ""
    protected: str | None = None  # snapshot id newly (or still) pinned
    unprotected: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    prune_skipped_reason: str | None = None
    protect_skipped_reason: str | None = None
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "group": self.group,
            "protected": self.protected,
            "protect_skipped_reason": self.protect_skipped_reason,
            "unprotected": self.unprotected,
            "pruned": self.pruned,
            "prune_skipped_reason": self.prune_skipped_reason,
            "dry_run": self.dry_run,
        }


def validate_retention(retention: Any) -> dict[str, Any]:
    """Return a normalised copy, or raise ``ValueError`` with a readable reason.

    Shared by the API (400 on bad input) and :func:`enforce_retention` (so a
    hand-edited services.yaml with a typo fails loudly instead of pruning with
    a silently-ignored key).
    """
    if retention is None:
        return {}
    if not isinstance(retention, dict):
        raise ValueError("retention must be an object")
    unknown = set(retention) - RETENTION_KEYS
    if unknown:
        raise ValueError(
            f"unknown retention key(s) {sorted(unknown)}; allowed: {sorted(RETENTION_KEYS)}"
        )
    out: dict[str, Any] = {}
    for key in COUNT_KEYS:
        if key not in retention or retention[key] is None:
            continue
        value = retention[key]
        # bool is an int subclass; `keep_daily: true` is a typo, not "1".
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"retention.{key} must be a non-negative integer, got {value!r}")
        out[key] = value
    for key in FLAG_KEYS:
        if key not in retention:
            continue
        if not isinstance(retention[key], bool):
            raise ValueError(f"retention.{key} must be true or false, got {retention[key]!r}")
        out[key] = retention[key]
    return out


def _verified_snapshot_ids(session: Session, slug: str) -> set[str]:
    """Snapshot ids our own verify step marked good.

    PBS's ``verification`` field is the primary source, but a fresh verify can
    lag the snapshot listing, and the dry-run adapter never sets it — the
    pipeline's ``BackupRecord.verified`` closes that gap.
    """
    rows = session.exec(
        select(BackupRecord.pbs_snapshot_id).where(
            BackupRecord.service_slug == slug, BackupRecord.verified == True  # noqa: E712
        )
    ).all()
    return set(rows)


async def enforce_retention(
    session: Session,
    pbs: PbsAdapter,
    policy: BackupPolicy,
    slug: str,
    *,
    settings: Settings,
    actor: str = "scheduler",
) -> RetentionResult:
    """Apply ``policy.retention`` to ``slug``'s PBS backup group.

    Caller commits. Raises :class:`RetentionError` for a policy/service that
    cannot be mapped (bad retention shape, no guest); adapter errors propagate.
    """
    retention = validate_retention(policy.retention)
    service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if service is None:
        raise RetentionError(f"no service registered with slug {slug!r}")
    pbs_type = _KIND_TO_PBS_TYPE.get(service.guest_kind)
    if pbs_type is None or service.guest_id is None:
        raise RetentionError(f"service {slug!r} has no Proxmox guest, so no PBS backup group")

    backup_id = str(service.guest_id)
    datastore = settings.pbs_datastore
    result = RetentionResult(slug=slug, group=f"{pbs_type}/{backup_id}", dry_run=settings.dry_run)

    # -- 1. pin the latest verified snapshot ---------------------------------
    if retention.get("protect_latest_verified") and not settings.pbs_protect_enabled:
        # The live adapter would turn set_protected into a logged no-op; don't
        # also record a pin in the DB that PBS never received.
        result.protect_skipped_reason = "PBS_PROTECT_ENABLED=false"
        log.warning(
            "retention.protect_disabled",
            service=slug,
            group=result.group,
            impact="protect_latest_verified is ignored; prune runs with no pinned "
            "known-good snapshot. See docs/api_tokens.md.",
        )
    elif retention.get("protect_latest_verified"):
        snapshots = await pbs.list_snapshots(datastore, backup_type=pbs_type, backup_id=backup_id)
        verified_ids = _verified_snapshot_ids(session, slug)
        verified = [s for s in snapshots if s.verified or s.snapshot_id in verified_ids]
        latest: Snapshot | None = max(verified, key=lambda s: s.backup_time, default=None)

        if latest is None:
            log.warning(
                "retention.no_verified_snapshot",
                service=slug,
                group=result.group,
                impact="Nothing is pinned; prune is free to remove any snapshot in "
                "this group within the keep_* counts.",
            )
        else:
            if not latest.protected:
                await pbs.set_protected(
                    datastore,
                    backup_type=pbs_type,
                    backup_id=backup_id,
                    backup_time=latest.backup_time,
                    protected=True,
                )
            result.protected = latest.snapshot_id
            _move_db_pin(session, slug, latest, result)
            await _unpin_previous(pbs, datastore, pbs_type, backup_id, snapshots, result)

    # -- 2. prune ------------------------------------------------------------
    counts = {k: retention[k] for k in COUNT_KEYS if k in retention}
    if not counts:
        # PBS treats "no keep-* options" as keep-everything, but say so rather
        # than send a request whose meaning depends on that default.
        result.prune_skipped_reason = "policy sets no keep_* counts"
    else:
        removed = await pbs.prune(
            datastore=datastore,
            backup_type=pbs_type,
            backup_id=backup_id,
            dry_run=settings.dry_run,
            **counts,
        )
        result.pruned = [s.snapshot_id for s in removed]

    audit.record(
        session,
        actor=actor,
        action="backup.retention",
        target=f"service:{slug}",
        details={"policy": policy.name, "retention": retention, **result.as_dict()},
    )
    log.info("retention.enforced", policy=policy.name, **result.as_dict())
    return result


def _move_db_pin(session: Session, slug: str, latest: Snapshot, result: RetentionResult) -> None:
    """Mirror the new pin onto ``BackupRecord.protected``; collect stale pins."""
    records = session.exec(
        select(BackupRecord).where(
            BackupRecord.service_slug == slug,
            (BackupRecord.protected == True)  # noqa: E712
            | (BackupRecord.pbs_snapshot_id == latest.snapshot_id),
        )
    ).all()
    for rec in records:
        should_pin = rec.pbs_snapshot_id == latest.snapshot_id
        if rec.protected and not should_pin:
            result.unprotected.append(rec.pbs_snapshot_id)
        rec.protected = should_pin
        session.add(rec)


async def _unpin_previous(
    pbs: PbsAdapter,
    datastore: str,
    pbs_type: str,
    backup_id: str,
    snapshots: list[Snapshot],
    result: RetentionResult,
) -> None:
    """Unpin, in PBS, the snapshots we previously pinned and just superseded."""
    by_id = {s.snapshot_id: s for s in snapshots}
    for snapshot_id in result.unprotected:
        snap = by_id.get(snapshot_id)
        if snap is None:
            continue  # already gone from PBS; the DB flag was cleared above
        await pbs.set_protected(
            datastore,
            backup_type=pbs_type,
            backup_id=backup_id,
            backup_time=snap.backup_time,
            protected=False,
        )
