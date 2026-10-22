"""Restoring a guest from its latest verified backup - always behind an approval.

A restore OVERWRITES the guest with an older copy of it. So nothing here runs
without a :class:`~orchestrator.db.models.PendingApproval` (kind REVERT) that a
logged-in admin approved: the recovery loop and the service page only *propose*;
the approval page is where a person says yes. The payload of the approval is the
exact action - it is never re-derived when it runs.

Steps: stop the guest -> restore the backup over it -> start it -> wait for HEALTHY.
Needs the restore token (PROXMOX_RESTORE_TOKEN_ID/SECRET), which the routine token
deliberately is not.
"""

from __future__ import annotations

import contextlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.proxmox.base import ProxmoxAdapter
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import (
    BackupRecord,
    Node,
    PendingApproval,
    PipelineRun,
    Service,
)
from orchestrator.domain.enums import (
    ApprovalKind,
    ApprovalStatus,
    AuditResult,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.health.engine import HealthEngine
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.steps import StepRecorder
from orchestrator.registry.tree import TreeIndex, effective_guest

log = get_logger(__name__)


class RestoreError(Exception):
    """The restore cannot be proposed, or did not succeed (message is shown)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def latest_verified_backup(session: Session, service: Service) -> BackupRecord | None:
    return session.exec(
        select(BackupRecord)
        .where(BackupRecord.service_slug == service.slug, col(BackupRecord.verified).is_(True))
        .order_by(col(BackupRecord.created_at).desc())
        .limit(1)
    ).first()


def create_proposal(
    session: Session,
    settings: Settings,
    service: Service,
    *,
    requested_by: str,
    reason: str,
    record: BackupRecord | None = None,
) -> PendingApproval:
    """Record "restore ``service`` from its latest verified backup" for a person to
    approve. Only a root service with a guest and a verified backup qualifies."""
    tree = TreeIndex.build(session)
    root = tree.root(service)
    if root.slug != service.slug:
        raise RestoreError(
            f"{service.slug!r} is a sub-service: a restore replaces a whole guest, so "
            f"propose it for {root.slug!r}"
        )
    eg = tree.effective_guest(service)
    node = session.get(Node, eg.node_id) if eg.node_id is not None else None
    if node is None or not eg.has_guest or eg.guest_id is None:
        raise RestoreError(f"{service.slug!r} has no Proxmox guest to restore")
    if service.backup_excluded:
        raise RestoreError(f"{service.slug!r} is excluded from backups, so it has none to restore")
    record = record or latest_verified_backup(session, service)
    if record is None:
        raise RestoreError(f"{service.slug!r} has no VERIFIED backup to restore from")

    # One open proposal per service is enough.
    for old in session.exec(
        select(PendingApproval).where(
            PendingApproval.kind == ApprovalKind.REVERT,
            PendingApproval.status == ApprovalStatus.PENDING,
        )
    ).all():
        if (old.payload or {}).get("service") == service.slug:
            old.status = ApprovalStatus.EXPIRED
            session.add(old)

    approval = PendingApproval(
        kind=ApprovalKind.REVERT,
        token=secrets.token_urlsafe(32),
        payload={
            "service": service.slug,
            "name": service.name,
            "node": node.name,
            "guest_kind": eg.kind.value,
            "vmid": eg.guest_id,
            "backup_record_id": record.id,
            "snapshot": record.pbs_snapshot_id,
            "snapshot_at": record.created_at.isoformat(),
            "reason": reason,
        },
        requested_by=requested_by,
        expires_at=_now() + timedelta(hours=max(1, settings.approval_ttl_hours)),
    )
    session.add(approval)
    session.flush()
    audit.record(
        session,
        actor=requested_by,
        action="restore.proposed",
        target=f"service:{service.slug}",
        details={"snapshot": record.pbs_snapshot_id, "reason": reason},
    )
    return approval


def approval_url(settings: Settings, token: str) -> str | None:
    base = (settings.external_url or settings.orchestrator_public_url).strip().rstrip("/")
    return f"{base}/approvals/{token}" if base else None


def is_open(approval: PendingApproval) -> bool:
    return approval.status is ApprovalStatus.PENDING and _aware(approval.expires_at) > _now()


class RestorePipeline:
    def __init__(
        self, *, proxmox: ProxmoxAdapter, settings: Settings, health: HealthEngine | None = None
    ) -> None:
        self._proxmox = proxmox
        self._settings = settings
        self._health = health or HealthEngine(settings=settings, proxmox=proxmox)

    def start(self, session: Session, approval: PendingApproval, actor: str) -> PipelineRun:
        """Mark the approval APPROVED and create the RUNNING run (sync, DB only)."""
        slug = str(approval.payload["service"])
        service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        if service is None:
            raise RestoreError(f"service {slug!r} no longer exists")
        correlation_id = new_correlation_id()
        run = PipelineRun(
            kind=PipelineKind.RESTORE,
            correlation_id=correlation_id,
            service_id=service.id,
            status=PipelineStatus.RUNNING,
        )
        session.add(run)
        approval.status = ApprovalStatus.APPROVED
        approval.approved_by = actor
        approval.approved_at = _now()
        session.add(approval)
        session.commit()
        session.refresh(run)
        approval.payload = {**approval.payload, "run_id": run.id}
        session.add(approval)
        audit.record(
            session,
            actor=actor,
            action="restore.start",
            target=f"service:{slug}",
            correlation_id=correlation_id,
            details={"run_id": run.id, "snapshot": approval.payload.get("snapshot")},
        )
        session.commit()
        return run

    async def execute(self, run_id: int, approval_id: int, actor: str) -> None:
        """Run to completion in a session of its own; never raises (a failure is
        recorded on the run and announced)."""
        from orchestrator.db.session import session_scope

        with session_scope() as session:
            run = session.get(PipelineRun, run_id)
            approval = session.get(PendingApproval, approval_id)
            if run is None or approval is None:
                return
            with contextlib.suppress(RestoreError, AdapterError):
                await self._run(session, run, approval, actor)

    async def _run(
        self, session: Session, run: PipelineRun, approval: PendingApproval, actor: str
    ) -> None:
        payload = approval.payload
        slug = str(payload["service"])
        steps = StepRecorder(session, run)
        service = session.exec(select(Service).where(Service.slug == slug)).one()
        try:
            record = session.get(BackupRecord, payload["backup_record_id"])
            if record is None or not record.verified:
                raise RestoreError("the backup this was proposed from is gone or not verified")
            eg = effective_guest(session, service)
            node = session.get(Node, eg.node_id) if eg.node_id is not None else None
            if node is None or eg.guest_id is None or eg.kind is GuestKind.NONE:
                raise RestoreError(f"{slug!r} has no Proxmox guest to restore")
            if self._backup_running(session, service):
                raise RestoreError(f"a backup of {slug!r} is running - not restoring over it")
            archive = f"{self._settings.pve_backup_storage}:backup/{record.pbs_snapshot_id}"

            await self._stop_guest(steps, node.name, eg.kind, eg.guest_id)
            started = _now()
            handle = await self._proxmox.restore_guest(
                node=node.name, vmid=eg.guest_id, kind=eg.kind, archive=archive
            )
            task = await self._proxmox.wait_for_task(
                node.name, handle.upid, timeout_s=self._settings.backup_task_timeout_s
            )
            if not task.succeeded:
                raise RestoreError(
                    f"Proxmox could not restore {archive}: {task.exit_status or 'unknown'}"
                )
            steps.record(
                "restore",
                "ok",
                started_at=started,
                detail={"archive": archive, "upid": handle.upid},
            )
            await self._start_guest(steps, node.name, eg.kind, eg.guest_id)
            await self._wait_healthy(session, steps, service)

            self._finish(session, run, PipelineStatus.SUCCEEDED)
            approval.status = ApprovalStatus.CONSUMED
            session.add(approval)
            audit.record(
                session,
                actor=actor,
                action="restore.succeeded",
                target=f"service:{slug}",
                correlation_id=run.correlation_id,
                details={"run_id": run.id, "snapshot": record.pbs_snapshot_id},
            )
            session.commit()
            await announce(
                session,
                level="service",
                key=slug,
                name=service.name,
                event="restore_succeeded",
                severity="info",
                title=f"{service.name} was restored from {record.pbs_snapshot_id}",
                body=f"Approved by {actor}. The service is healthy again.",
                scope={"slug": slug, "group": service.group},
            )
        except (RestoreError, AdapterError) as exc:
            steps.record(
                "aborted", "failed", detail={"error": str(exc), "type": type(exc).__name__}
            )
            self._finish(session, run, PipelineStatus.FAILED, error=str(exc))
            approval.status = ApprovalStatus.CONSUMED
            session.add(approval)
            audit.record(
                session,
                actor=actor,
                action="restore.failed",
                target=f"service:{slug}",
                result=AuditResult.ERROR,
                correlation_id=run.correlation_id,
                details={"run_id": run.id, "error": str(exc)},
            )
            session.commit()
            await announce(
                session,
                level="service",
                key=slug,
                name=service.name,
                event="restore_failed",
                severity="critical",
                title=f"Restore of {service.name} FAILED",
                body=str(exc)[:1500],
                scope={"slug": slug, "group": service.group},
            )
            raise

    # -- steps -----------------------------------------------------------------

    async def _stop_guest(self, steps: StepRecorder, node: str, kind: GuestKind, vmid: int) -> None:
        guests = await self._proxmox.list_guests(node)
        guest = next((g for g in guests if g.vmid == vmid), None)
        if guest is None or guest.status != "running":
            steps.record("guest_stop", "already_stopped", detail={"vmid": vmid})
            return
        started = _now()
        handle = await self._proxmox.stop_guest(node, vmid, kind)
        task = await self._proxmox.wait_for_task(node, handle.upid, timeout_s=300)
        if not task.succeeded:
            raise RestoreError(f"could not stop guest {vmid} before restoring: {task.exit_status}")
        steps.record("guest_stop", "ok", started_at=started, detail={"vmid": vmid})

    async def _start_guest(
        self, steps: StepRecorder, node: str, kind: GuestKind, vmid: int
    ) -> None:
        started = _now()
        handle = await self._proxmox.start_guest(node, vmid, kind)
        task = await self._proxmox.wait_for_task(node, handle.upid, timeout_s=300)
        if not task.succeeded:
            raise RestoreError(f"restored, but guest {vmid} would not start: {task.exit_status}")
        steps.record("guest_start", "ok", started_at=started, detail={"vmid": vmid})

    async def _wait_healthy(self, session: Session, steps: StepRecorder, service: Service) -> None:
        import asyncio
        import time

        started = _now()
        deadline = time.monotonic() + self._settings.wake_timeout_s
        while True:
            verdict = await self._health.scan(session, service)
            if verdict.state is HealthState.HEALTHY:
                steps.record(
                    "health_poll", "ok", started_at=started, detail={"reason": verdict.reason}
                )
                return
            if time.monotonic() >= deadline:
                raise RestoreError(
                    f"the backup was restored and the guest started, but {service.slug!r} did "
                    f"not become HEALTHY in time - last verdict: {verdict.state.value} "
                    f"({verdict.reason})"
                )
            await asyncio.sleep(self._settings.wake_poll_interval_s)

    @staticmethod
    def _backup_running(session: Session, service: Service) -> bool:
        return (
            session.exec(
                select(PipelineRun.id)
                .where(
                    PipelineRun.service_id == service.id,
                    PipelineRun.kind == PipelineKind.BACKUP,
                    PipelineRun.status == PipelineStatus.RUNNING,
                )
                .limit(1)
            ).first()
            is not None
        )

    @staticmethod
    def _finish(
        session: Session, run: PipelineRun, status: PipelineStatus, *, error: str | None = None
    ) -> None:
        run.status = status
        run.finished_at = _now()
        run.error = error
        session.add(run)
        session.commit()


def approval_view(
    session: Session, settings: Settings, approval: PendingApproval
) -> dict[str, Any]:
    """What the approval page shows."""
    payload = dict(approval.payload or {})
    run = session.get(PipelineRun, payload["run_id"]) if payload.get("run_id") else None
    open_ = is_open(approval)
    status = approval.status.value
    if approval.status is ApprovalStatus.PENDING and not open_:
        status = "expired"
    return {
        "token": approval.token,
        "status": status,
        "open": open_,
        "requested_by": approval.requested_by,
        "requested_at": _aware(approval.requested_at).isoformat(),
        "expires_at": _aware(approval.expires_at).isoformat(),
        "approved_by": approval.approved_by,
        "payload": payload,
        "url": approval_url(settings, approval.token),
        "run": (
            {
                "id": run.id,
                "status": run.status.value,
                "error": run.error,
                "steps": run.steps,
            }
            if run is not None
            else None
        ),
    }
