"""Manual backup trigger + pipeline run inspection.

Phase 2 runs the backup **synchronously** so the caller sees the real outcome —
this is a testing surface. Phase 6's scheduler will drive the same pipeline in
the background; the run rows are identical either way.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import desc, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.db.models import BackupRecord, PipelineRun
from orchestrator.pipelines.backup import BackupError, BackupPipeline
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["backup"])


def _run_to_dict(run: PipelineRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "kind": run.kind.value,
        "status": run.status.value,
        "correlation_id": run.correlation_id,
        "service_id": run.service_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "steps": run.steps,
    }


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
async def list_runs(session: SessionDep, limit: int = 20) -> list[dict[str, Any]]:
    runs = session.exec(
        select(PipelineRun).order_by(desc(PipelineRun.started_at)).limit(limit)
    ).all()
    return [_run_to_dict(r) for r in runs]


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
