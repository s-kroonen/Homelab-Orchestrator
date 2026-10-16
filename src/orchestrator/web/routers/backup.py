"""Manual backup trigger + pipeline run inspection.

Phase 2 runs the backup **synchronously** so the caller sees the real outcome —
this is a testing surface. Phase 6's scheduler will drive the same pipeline in
the background; the run rows are identical either way.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, col, desc, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.db.models import BackupRecord, PipelineRun, Service
from orchestrator.pipelines.backup import BackupError, BackupPipeline
from orchestrator.web.auth import require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["backup"], dependencies=[Depends(require_admin)])


def _run_to_dict(run: PipelineRun, service_slug: str | None = None) -> dict[str, Any]:
    return {
        "id": run.id,
        "kind": run.kind.value,
        "status": run.status.value,
        "correlation_id": run.correlation_id,
        "service_id": run.service_id,
        "service_slug": service_slug,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "steps": run.steps,
    }


def _service_slugs(session: Session, service_ids: set[int]) -> dict[int, str]:
    if not service_ids:
        return {}
    rows = session.exec(
        select(Service.id, Service.slug).where(col(Service.id).in_(service_ids))
    ).all()
    return {sid: slug for sid, slug in rows if sid is not None}


@router.post("/backup/{slug}")
async def trigger_backup(
    slug: str,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    verify: bool = Query(True, description="Run a PBS verify after the dump"),
) -> dict[str, Any]:
    pipeline = BackupPipeline(proxmox=adapters.proxmox, pbs=adapters.pbs, settings=settings)
    try:
        run = await pipeline.run_for_service(session, slug, actor="api", verify=verify)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AdapterError as exc:
        # 502: we're fine, the thing behind us isn't.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _run_to_dict(run)


@router.get("/runs")
async def list_runs(
    session: SessionDep,
    limit: int = 20,
    policy_id: int | None = Query(
        None,
        description=(
            "Filter to runs for every service currently attached to this backup "
            "policy (Service.backup_policy_id) — a policy's own run history "
            "across every service it covers, not just one. Services that were "
            "attached when a run happened but have since been moved or detached "
            "are not retroactively included: this reflects CURRENT membership, "
            "the same single source of truth backup_policies.py's "
            "_attached_services uses, not a historical snapshot."
        ),
    ),
) -> list[dict[str, Any]]:
    stmt = select(PipelineRun).order_by(desc(PipelineRun.started_at)).limit(limit)
    if policy_id is not None:
        # PipelineRun has no policy_id of its own — only service_id — so this
        # resolves "every service this policy currently covers"
        # (Service.backup_policy_id, the same single source of truth
        # backup_policies.py's _attached_services uses) to a set of ids
        # first, rather than adding a policy_id column to PipelineRun that
        # would need backfilling for every historical run. A dedicated
        # BackupRecord-based endpoint was considered instead — BackupRecord
        # already HAS policy_id — but it only covers successful backups
        # that got as far as writing one, and a failed run is exactly what
        # this view most needs to surface.
        covered_ids = session.exec(
            select(Service.id).where(Service.backup_policy_id == policy_id)
        ).all()
        stmt = stmt.where(col(PipelineRun.service_id).in_(covered_ids))
    runs = session.exec(stmt).all()
    slugs = _service_slugs(session, {r.service_id for r in runs if r.service_id is not None})
    return [
        _run_to_dict(r, slugs.get(r.service_id) if r.service_id is not None else None) for r in runs
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: int, session: SessionDep) -> dict[str, Any]:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no pipeline run with id {run_id}")
    return _run_to_dict(run)


@router.get("/backups")
async def list_backups(
    session: SessionDep, slug: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = select(BackupRecord).order_by(desc(BackupRecord.created_at)).limit(limit)
    if slug:
        stmt = stmt.where(BackupRecord.service_slug == slug)
    records = session.exec(stmt).all()
    return [
        {
            "id": r.id,
            "service_slug": r.service_slug,
            "pbs_snapshot_id": r.pbs_snapshot_id,
            "size_bytes": r.size_bytes,
            "verified": r.verified,
            "verified_at": r.verified_at.isoformat() if r.verified_at else None,
            "protected": r.protected,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "pipeline_run_id": r.pipeline_run_id,
        }
        for r in records
    ]
