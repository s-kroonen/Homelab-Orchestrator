"""Backup policy CRUD (phase 6).

``BackupPolicy`` is YAML-owned, but like ``Service`` it is edited live in the
DB: these endpoints only touch the DB, and the existing ``/api/registry/save``
and ``/api/registry/reset`` round-trip the result to and from services.yaml.
Nothing here writes YAML.

Every mutation re-syncs the running scheduler's job set, so a schedule change
takes effect immediately rather than at the next restart.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from orchestrator.audit import log as audit
from orchestrator.db.models import BackupPolicy, BackupRecord, Service
from orchestrator.domain.enums import BackupMode
from orchestrator.scheduler import BackupScheduler, next_fire_time, parse_cron
from orchestrator.scheduler.retention import validate_retention
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import SessionDep

router = APIRouter(
    prefix="/api/backup-policies",
    tags=["backup-policies"],
    dependencies=[Depends(require_admin)],
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PolicyCreate(BaseModel):
    # Loose types on purpose: shape errors become readable 400s from the
    # validators below instead of pydantic's 422 wall of locations.
    model_config = ConfigDict(extra="forbid")

    name: str
    schedule_cron: str = ""
    mode: str = BackupMode.SNAPSHOT.value
    retention: Any = None
    targets: Any = None


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_cron: str | None = None
    mode: str | None = None
    retention: Any = None
    targets: Any = None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise _bad(
            "name must be 1-64 characters of letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return name


def _validate_cron(expr: str) -> str:
    expr = expr.strip()
    if expr:
        try:
            parse_cron(expr)
        except ValueError as exc:
            raise _bad(f"{exc}. Use 5 fields: minute hour day month weekday") from exc
    return expr


def _validate_mode(mode: str) -> BackupMode:
    try:
        return BackupMode(mode)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in BackupMode)
        raise _bad(f"mode must be one of: {allowed}") from exc


def _validate_retention(retention: Any) -> dict[str, Any]:
    try:
        return validate_retention(retention)
    except ValueError as exc:
        raise _bad(str(exc)) from exc


def _validate_targets(session: Session, targets: Any) -> dict[str, Any]:
    if targets is None:
        return {}
    if not isinstance(targets, dict):
        raise _bad('targets must be an object like {"services": ["slug"]}')
    unknown = set(targets) - {"services"}
    if unknown:
        raise _bad(f"unknown targets key(s) {sorted(unknown)}; only 'services' is supported")
    if "services" not in targets:
        return {}
    slugs = targets["services"]
    if not isinstance(slugs, list) or not all(isinstance(s, str) for s in slugs):
        raise _bad("targets.services must be a list of service slugs")
    if len(set(slugs)) != len(slugs):
        raise _bad("targets.services lists a service more than once")
    known = set(session.exec(select(Service.slug)).all())
    missing = [s for s in slugs if s not in known]
    if missing:
        raise _bad(f"targets.services references unknown service(s): {missing}")
    return {"services": slugs}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _scheduler(request: Request) -> BackupScheduler | None:
    return getattr(request.app.state, "scheduler", None)


def _resync(request: Request) -> None:
    scheduler = _scheduler(request)
    if scheduler is not None:
        scheduler.sync_jobs()


def _get_or_404(session: Session, name: str) -> BackupPolicy:
    policy = session.exec(select(BackupPolicy).where(BackupPolicy.name == name)).one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail=f"no backup policy named {name!r}")
    return policy


def schedule_info(policy: BackupPolicy, scheduler: BackupScheduler | None) -> dict[str, Any]:
    """Next run + whether a live job exists. Shared with the dashboard page."""
    info: dict[str, Any] = {
        "scheduler_running": bool(scheduler and scheduler.running),
        "scheduled": False,
        "next_run": None,
        "schedule_error": None,
    }
    if not policy.schedule_cron:
        return info
    if scheduler is not None:
        info["schedule_error"] = scheduler.errors.get(policy.name)
        when = scheduler.next_run_time(policy.name)
        info["scheduled"] = when is not None
        info["next_run"] = when.isoformat() if when else None
        return info
    # No scheduler (disabled or failed to start): still show when it *would*
    # run, so the operator can sanity-check the cron, but scheduled=False.
    try:
        when = next_fire_time(policy.schedule_cron)
        info["next_run"] = when.isoformat() if when else None
    except ValueError as exc:
        info["schedule_error"] = str(exc)
    return info


def policy_to_dict(
    session: Session, policy: BackupPolicy, scheduler: BackupScheduler | None
) -> dict[str, Any]:
    attached = (
        session.exec(
            select(Service.slug).where(Service.backup_policy_id == policy.id).order_by(Service.slug)
        ).all()
        if policy.id is not None
        else []
    )
    return {
        "name": policy.name,
        "schedule_cron": policy.schedule_cron,
        "mode": policy.mode.value,
        "retention": policy.retention,
        "targets": policy.targets,
        # Services that name this policy via `backup_policy:` in services.yaml.
        # The scheduler backs these up too; they are edited on the service.
        "attached_services": list(attached),
        "created_at": policy.created_at.isoformat() if policy.created_at else None,
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
        **schedule_info(policy, scheduler),
    }


def _snapshot(policy: BackupPolicy) -> dict[str, Any]:
    return {
        "schedule_cron": policy.schedule_cron,
        "mode": policy.mode.value,
        "retention": policy.retention,
        "targets": policy.targets,
    }


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_policies(request: Request, session: SessionDep) -> list[dict[str, Any]]:
    policies = session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
    scheduler = _scheduler(request)
    return [policy_to_dict(session, p, scheduler) for p in policies]


@router.get("/{name}")
async def get_policy(name: str, request: Request, session: SessionDep) -> dict[str, Any]:
    return policy_to_dict(session, _get_or_404(session, name), _scheduler(request))


@router.post("", status_code=201)
async def create_policy(
    body: PolicyCreate,
    request: Request,
    session: SessionDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    name = _validate_name(body.name)
    if session.exec(select(BackupPolicy).where(BackupPolicy.name == name)).one_or_none():
        raise HTTPException(status_code=409, detail=f"a backup policy named {name!r} exists")
    policy = BackupPolicy(
        name=name,
        schedule_cron=_validate_cron(body.schedule_cron),
        mode=_validate_mode(body.mode),
        retention=_validate_retention(body.retention),
        targets=_validate_targets(session, body.targets),
    )
    session.add(policy)
    audit.record(
        session,
        actor=admin.username,
        action="backup_policy.created",
        target=f"policy:{name}",
        details=_snapshot(policy),
    )
    session.commit()
    session.refresh(policy)
    _resync(request)
    return policy_to_dict(session, policy, _scheduler(request))


@router.put("/{name}")
async def update_policy(
    name: str,
    body: PolicyUpdate,
    request: Request,
    session: SessionDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    policy = _get_or_404(session, name)
    before = _snapshot(policy)
    fields = body.model_fields_set
    if "schedule_cron" in fields:
        policy.schedule_cron = _validate_cron(body.schedule_cron or "")
    if "mode" in fields:
        policy.mode = _validate_mode(body.mode or "")
    if "retention" in fields:
        policy.retention = _validate_retention(body.retention)
    if "targets" in fields:
        policy.targets = _validate_targets(session, body.targets)
    policy.updated_at = datetime.now(UTC)
    session.add(policy)
    audit.record(
        session,
        actor=admin.username,
        action="backup_policy.updated",
        target=f"policy:{name}",
        details={"before": before, "after": _snapshot(policy)},
    )
    session.commit()
    session.refresh(policy)
    _resync(request)
    return policy_to_dict(session, policy, _scheduler(request))


@router.delete("/{name}")
async def delete_policy(
    name: str,
    request: Request,
    session: SessionDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    policy = _get_or_404(session, name)
    # Detach explicitly rather than relying on ON DELETE SET NULL: SQLite only
    # enforces FK actions with PRAGMA foreign_keys=ON, and a service left
    # pointing at a vanished policy id would make the next Save write a
    # dangling `backup_policy:` reference into services.yaml.
    detached = session.exec(select(Service).where(Service.backup_policy_id == policy.id)).all()
    for svc in detached:
        svc.backup_policy_id = None
        session.add(svc)
    for rec in session.exec(select(BackupRecord).where(BackupRecord.policy_id == policy.id)).all():
        rec.policy_id = None
        session.add(rec)
    audit.record(
        session,
        actor=admin.username,
        action="backup_policy.deleted",
        target=f"policy:{name}",
        details={**_snapshot(policy), "detached_services": [s.slug for s in detached]},
    )
    session.delete(policy)
    session.commit()
    _resync(request)
    return {"status": "deleted", "name": name, "detached_services": [s.slug for s in detached]}
