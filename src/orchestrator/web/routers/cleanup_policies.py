"""Cleanup policies (OR-96, docs/cleanup.md) and the backup queue they run in.

Like backup policies, cleanup policies are YAML-owned but edited live in the DB: these
endpoints touch the DB only, and /api/registry/save round-trips them to services.yaml.
Every mutation re-syncs the scheduler, so a schedule change takes effect at once.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from orchestrator.audit import log as audit
from orchestrator.db.models import BackupPolicy, CleanupPolicy, PipelineRun
from orchestrator.domain.enums import BackupWakeMode, CleanupGcMode, PipelineKind
from orchestrator.registry.backup_plan import clean_slug_list, resolve_prerequisites
from orchestrator.scheduler import BackupScheduler, next_fire_time, parse_cron
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import SessionDep

router = APIRouter(
    prefix="/api/cleanup-policies",
    tags=["cleanup-policies"],
    dependencies=[Depends(require_admin)],
)
datastore_router = APIRouter(
    prefix="/api/pbs-datastore", tags=["cleanup-policies"], dependencies=[Depends(require_admin)]
)
queue_router = APIRouter(
    prefix="/api/backup-queue", tags=["cleanup-policies"], dependencies=[Depends(require_admin)]
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: Cleanups started from the dashboard, kept so they are not garbage collected mid-run.
_RUNS: set[asyncio.Task[Any]] = set()


class CleanupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    datastore: str | None = None
    schedule_cron: str | None = None
    prune: bool | None = None
    prune_unowned_keep_last: int | None = None
    gc_mode: str | None = None
    gc_interval_h: int | None = None
    gc_above_pct: int | None = None
    max_wait_h: int | None = None
    requires: Any = None
    wake_mode: str | None = None
    restore_power_state: bool | None = None


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _scheduler(request: Request) -> BackupScheduler | None:
    return getattr(request.app.state, "scheduler", None)


def _resync(request: Request) -> None:
    scheduler = _scheduler(request)
    if scheduler is not None:
        scheduler.sync_jobs()


def _get_or_404(session: Session, name: str) -> CleanupPolicy:
    found = session.exec(select(CleanupPolicy).where(CleanupPolicy.name == name)).one_or_none()
    if found is None:
        raise HTTPException(status_code=404, detail=f"no cleanup policy named {name!r}")
    return found


def _int(raw: Any, field: str, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise _bad(f"{field} must be a whole number from {low} to {high}")
    return raw


def _apply(session: Session, c: CleanupPolicy, body: CleanupBody, fields: set[str]) -> None:
    if "datastore" in fields:
        c.datastore = (body.datastore or "").strip()
    if "schedule_cron" in fields:
        cron = (body.schedule_cron or "").strip()
        if cron:
            try:
                parse_cron(cron)
            except ValueError as exc:
                raise _bad(f"{exc}. Use 5 fields: minute hour day month weekday") from exc
        c.schedule_cron = cron
    if "prune" in fields:
        if body.prune is None:
            raise _bad("prune must be true or false")
        c.prune = body.prune
    if "prune_unowned_keep_last" in fields:
        keep = body.prune_unowned_keep_last
        c.prune_unowned_keep_last = (
            None if keep in (None, 0) else _int(keep, "prune_unowned_keep_last", 1, 1000)
        )
    if "gc_mode" in fields:
        try:
            c.gc_mode = CleanupGcMode(str(body.gc_mode or "").strip().lower())
        except ValueError:
            raise _bad("gc_mode must be interval, above or off") from None
    if "gc_interval_h" in fields:
        c.gc_interval_h = _int(body.gc_interval_h, "gc_interval_h", 0, 24 * 365)
    if "gc_above_pct" in fields:
        c.gc_above_pct = _int(body.gc_above_pct, "gc_above_pct", 1, 100)
    if "max_wait_h" in fields:
        c.max_wait_h = _int(body.max_wait_h, "max_wait_h", 1, 72)
    if "requires" in fields:
        if body.requires is not None and (
            not isinstance(body.requires, list)
            or not all(isinstance(x, str) for x in body.requires)
        ):
            raise _bad("requires must be a list of service slugs")
        cleaned = clean_slug_list(body.requires or [])
        from orchestrator.db.models import Service

        known = set(session.exec(select(Service.slug)).all())
        missing = [x for x in cleaned if x not in known]
        if missing:
            raise _bad(f"requires references unknown service(s): {', '.join(missing)}")
        c.requires = cleaned
    if "wake_mode" in fields:
        try:
            c.wake_mode = BackupWakeMode(str(body.wake_mode or "").strip().lower())
        except ValueError:
            raise _bad("wake_mode must be follow, always or never") from None
    if "restore_power_state" in fields:
        if body.restore_power_state is None:
            raise _bad("restore_power_state must be true or false")
        c.restore_power_state = body.restore_power_state
    c.updated_at = datetime.now(UTC)


def _last_run(session: Session, name: str) -> dict[str, Any] | None:
    runs = session.exec(
        select(PipelineRun)
        .where(PipelineRun.kind == PipelineKind.CLEANUP)
        .order_by(PipelineRun.started_at.desc())  # type: ignore[attr-defined]
        .limit(50)
    ).all()
    for run in runs:
        head = next((s for s in run.steps or [] if s.get("name") == "cleanup"), None)
        if head is None or head["detail"].get("policy") != name:
            continue
        by = {s["name"]: s for s in run.steps or []}
        return {
            "run_id": run.id,
            "status": run.status.value,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error": run.error,
            "removed": (by.get("prune") or {}).get("detail", {}).get("removed"),
            "gc": {
                "status": (by.get("gc") or {}).get("status"),
                **((by.get("gc") or {}).get("detail") or {}),
            },
        }
    return None


def cleanup_to_dict(
    session: Session, c: CleanupPolicy, scheduler: BackupScheduler | None
) -> dict[str, Any]:
    used_by = session.exec(select(BackupPolicy).where(BackupPolicy.cleanup_policy == c.name)).all()
    next_run = None
    schedule_error = None
    if c.schedule_cron:
        if scheduler is not None:
            schedule_error = scheduler.cleanup_errors.get(c.name)
            when = scheduler.next_cleanup_time(c.name)
        else:
            try:
                when = next_fire_time(c.schedule_cron)
            except ValueError as exc:
                when, schedule_error = None, str(exc)
        next_run = when.isoformat() if when else None
    return {
        "id": c.id,
        "name": c.name,
        "datastore": c.datastore,
        "schedule_cron": c.schedule_cron,
        "prune": c.prune,
        "prune_unowned_keep_last": c.prune_unowned_keep_last,
        "gc_mode": c.gc_mode.value,
        "gc_interval_h": c.gc_interval_h,
        "gc_above_pct": c.gc_above_pct,
        "max_wait_h": c.max_wait_h,
        "requires": list(c.requires or []),
        "requires_blockers": [
            {"slug": s.slug, "node": s.node, "blocker": s.blocker}
            for s in resolve_prerequisites(session, c.requires or [], c.wake_mode)
            if s.blocker
        ],
        "wake_mode": c.wake_mode.value,
        "restore_power_state": c.restore_power_state,
        "used_by": [{"policy": p.name, "when": p.cleanup_when.value} for p in used_by],
        "next_run": next_run,
        "schedule_error": schedule_error,
        "last_run": _last_run(session, c.name),
    }


@router.get("")
async def list_cleanups(request: Request, session: SessionDep) -> list[dict[str, Any]]:
    rows = session.exec(select(CleanupPolicy).order_by(CleanupPolicy.name)).all()
    return [cleanup_to_dict(session, c, _scheduler(request)) for c in rows]


@router.get("/{name}")
async def get_cleanup(name: str, request: Request, session: SessionDep) -> dict[str, Any]:
    return cleanup_to_dict(session, _get_or_404(session, name), _scheduler(request))


@router.post("", status_code=201)
async def create_cleanup(
    body: CleanupBody, request: Request, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    name = (body.name or "").strip()
    if not _NAME_RE.match(name):
        raise _bad(
            "name must be 1-64 characters of letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    if session.exec(select(CleanupPolicy).where(CleanupPolicy.name == name)).one_or_none():
        raise HTTPException(status_code=409, detail=f"a cleanup policy named {name!r} exists")
    c = CleanupPolicy(name=name)
    _apply(session, c, body, body.model_fields_set - {"name"})
    session.add(c)
    audit.record(
        session,
        actor=admin.username,
        action="cleanup_policy.created",
        target=f"cleanup:{name}",
        details=body.model_dump(exclude_unset=True),
    )
    session.commit()
    session.refresh(c)
    _resync(request)
    return cleanup_to_dict(session, c, _scheduler(request))


@router.put("/{name}")
async def update_cleanup(
    name: str, body: CleanupBody, request: Request, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    c = _get_or_404(session, name)
    if "name" in body.model_fields_set and (body.name or "") != name:
        raise _bad("a cleanup policy cannot be renamed; create a new one")
    _apply(session, c, body, body.model_fields_set - {"name"})
    session.add(c)
    audit.record(
        session,
        actor=admin.username,
        action="cleanup_policy.updated",
        target=f"cleanup:{name}",
        details=body.model_dump(exclude_unset=True),
    )
    session.commit()
    session.refresh(c)
    _resync(request)
    return cleanup_to_dict(session, c, _scheduler(request))


@router.delete("/{name}")
async def delete_cleanup(
    name: str, request: Request, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    c = _get_or_404(session, name)
    # A backup policy left naming it would make the next Save write a dangling reference.
    detached = session.exec(select(BackupPolicy).where(BackupPolicy.cleanup_policy == name)).all()
    for pol in detached:
        pol.cleanup_policy = ""
        session.add(pol)
    audit.record(
        session,
        actor=admin.username,
        action="cleanup_policy.deleted",
        target=f"cleanup:{name}",
        details={"detached_backup_policies": [p.name for p in detached]},
    )
    session.delete(c)
    session.commit()
    _resync(request)
    return {"status": "deleted", "name": name, "detached": [p.name for p in detached]}


@router.post("/{name}/run", status_code=202)
async def run_cleanup_now(
    name: str, request: Request, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Queue a cleanup now. Answers at once; the run shows in Activity as it happens."""
    _get_or_404(session, name)
    scheduler = _scheduler(request)
    if scheduler is None:
        raise HTTPException(409, "the backup scheduler is not running (SCHEDULER_ENABLED=false?)")
    audit.record(session, actor=admin.username, action="cleanup_policy.run_now",
                 target=f"cleanup:{name}")  # fmt: skip
    session.commit()
    task = asyncio.create_task(
        scheduler.run_cleanup(name, actor=admin.username, reason=f"run now by {admin.username}"),
        name=f"cleanup-run-{name}",
    )
    _RUNS.add(task)
    task.add_done_callback(_RUNS.discard)
    return {"queued": True, "cleanup": name}


@queue_router.get("")
async def get_queue(request: Request) -> dict[str, Any]:
    scheduler = _scheduler(request)
    if scheduler is None:
        return {"held": None, "jobs": [], "scheduler_running": False}
    return {**scheduler.queue(), "scheduler_running": scheduler.running}


@queue_router.delete("/{job_id}")
async def cancel_job(
    job_id: int, request: Request, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Cancel a waiting job, or stop a running cleanup's garbage collection."""
    scheduler = _scheduler(request)
    if scheduler is None:
        raise HTTPException(409, "the backup scheduler is not running")
    try:
        done = scheduler.cancel(job_id)
    except KeyError:
        raise HTTPException(404, f"no queued job {job_id}") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    audit.record(session, actor=admin.username, action="backup_queue.cancel",
                 target=f"job:{job_id}", details={"done": done})  # fmt: skip
    session.commit()
    return {"job": job_id, "done": done}


def _has_priv(perms: dict[str, Any], datastore: str, priv: str) -> bool | None:
    """Does the token hold ``priv`` on the datastore (or above it)? None when PBS did not say."""
    if not isinstance(perms, dict) or not perms:
        return None
    for path in (f"/datastore/{datastore}", "/datastore", "/"):
        entry = perms.get(path)
        if isinstance(entry, dict) and entry.get(priv):
            return True
    return False


@datastore_router.get("")
async def datastore_summary(
    request: Request, session: SessionDep, name: str | None = None
) -> dict[str, Any]:
    """One datastore, for Backups → Cleanup and the PBS source's Datastores tab: how full,
    PBS's last GC, what waits for the next, which cleanup policies clean it, PBS's own jobs
    (which fail while it is offline), and whether the token may prune and collect."""
    from orchestrator.config import get_settings

    settings = get_settings()
    ds = name or settings.pbs_datastore
    adapters = getattr(request.app.state, "adapters", None)
    out: dict[str, Any] = {"name": ds, "reachable": False, "error": None}
    cleaners = [
        c
        for c in session.exec(select(CleanupPolicy)).all()
        if (c.datastore or settings.pbs_datastore) == ds
    ]
    out["cleaned_by"] = [
        {
            "policy": c.name,
            "schedule_cron": c.schedule_cron,
            "used_by": [
                {"policy": p.name, "when": p.cleanup_when.value}
                for p in session.exec(
                    select(BackupPolicy).where(BackupPolicy.cleanup_policy == c.name)
                ).all()
            ],
        }
        for c in cleaners
    ]
    # The last cleanup's own record: what it removed, what it left waiting, unowned groups.
    last = None
    for c in cleaners:
        run = _last_run(session, c.name)
        if run and (last is None or run["started_at"] > last["started_at"]):
            last = {**run, "policy": c.name}
    out["last_cleanup"] = last
    if adapters is None:
        out["error"] = "the adapters are not ready"
        return out
    pbs = adapters.pbs
    try:
        st = await asyncio.wait_for(pbs.datastore_status(ds), 10)
        out.update(
            reachable=True,
            total_bytes=st.total_bytes,
            used_bytes=st.used_bytes,
            available_bytes=st.available_bytes,
            used_fraction=st.used_fraction,
        )
    except Exception as exc:
        out["error"] = str(exc)
    if out["reachable"]:
        try:
            gc = await asyncio.wait_for(pbs.garbage_collection_status(ds), 10)
            out["gc"] = {
                "upid": gc.upid,
                "removed_bytes": gc.removed_bytes,
                "pending_bytes": gc.pending_bytes,
            }
        except Exception as exc:
            out["gc"] = {"error": str(exc)}
    try:
        out["own_jobs"] = await asyncio.wait_for(pbs.own_jobs(ds), 10)
    except Exception as exc:
        out["own_jobs"] = []
        out["own_jobs_error"] = str(exc)
    try:
        perms = await asyncio.wait_for(pbs.effective_permissions(), 10)
        out["permissions"] = {
            "audit": _has_priv(perms, ds, "Datastore.Audit"),
            "modify": _has_priv(perms, ds, "Datastore.Modify"),
            "prune": _has_priv(perms, ds, "Datastore.Prune"),
        }
    except Exception as exc:
        out["permissions"] = {"error": str(exc)}
    return out
