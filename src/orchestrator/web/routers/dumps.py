"""Dumps of a service's own data: configuration, running one, the files, download.

``POST /api/services/{slug}/dump/run`` returns as soon as the run is recorded; poll
``GET /api/runs/{id}`` or watch Activity. Files are served only to a logged-in admin. See
docs/dumps.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlmodel import desc, select

from orchestrator.audit import log as audit
from orchestrator.db.models import DumpRecord, PipelineRun, Service
from orchestrator.domain.enums import PipelineKind
from orchestrator.dumps.pipeline import DumpPipeline, DumpRefused, dump_root, file_of, remove_record
from orchestrator.dumps.policy import describe, normalize_dump, restore_hint, target_of
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.wake_support import run_to_dict

router = APIRouter(prefix="/api", tags=["dumps"], dependencies=[Depends(require_admin)])

_REFUSED = {
    "unknown": status.HTTP_404_NOT_FOUND,
    "no_dump": status.HTTP_404_NOT_FOUND,
    "disabled": status.HTTP_423_LOCKED,
}


class ProtectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected: bool


def record_view(r: DumpRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "service": r.service_slug,
        "path": r.path,
        "file": r.path.rsplit("/", 1)[-1],
        "kind": r.kind,
        "size_bytes": r.size_bytes,
        "sha256": r.sha256,
        "verified": r.verified,
        "protected": r.protected,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "run_id": r.pipeline_run_id,
    }


def _service(session: SessionDep, slug: str) -> Service:
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    return svc


def records_of(session: SessionDep, slug: str, limit: int = 50) -> list[DumpRecord]:
    return list(
        session.exec(
            select(DumpRecord)
            .where(DumpRecord.service_slug == slug)
            .order_by(desc(DumpRecord.created_at), desc(DumpRecord.id))
            .limit(max(1, min(limit, 200)))
        ).all()
    )


def last_run(session: SessionDep, svc: Service) -> dict[str, Any] | None:
    run = session.exec(
        select(PipelineRun)
        .where(PipelineRun.kind == PipelineKind.DUMP, PipelineRun.service_id == svc.id)
        .order_by(desc(PipelineRun.started_at))
        .limit(1)
    ).first()
    return {"service": svc.slug, **run_to_dict(run)} if run else None


@router.get("/services/{slug}/dump")
async def get_dump(slug: str, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    """The service's dump configuration, its files and its last run."""
    svc = _service(session, slug)
    try:
        cfg = normalize_dump(svc.dump)
    except ValueError as exc:
        return {"configured": True, "error": str(exc), "config": dict(svc.dump or {})}
    records = records_of(session, slug)
    hint = ""
    if cfg:
        try:
            _, container = target_of(cfg, svc.controller or {})
        except (KeyError, ValueError):
            container = cfg.get("container") or ""
        hint = restore_hint(cfg, container, records[0].path.rsplit("/", 1)[-1] if records else "")
    return {
        "configured": bool(cfg),
        "config": cfg,
        "describe": describe(cfg),
        "directory": str(dump_root(settings) / slug),
        "records": [record_view(r) for r in records],
        "last_run": last_run(session, svc),
        "restore_hint": hint,
    }


@router.post("/services/{slug}/dump/run", status_code=status.HTTP_202_ACCEPTED)
async def run_dump(
    slug: str,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    pipeline = DumpPipeline(adapters, settings)
    try:
        run = pipeline.start(session, slug, admin.username)
    except DumpRefused as exc:
        raise HTTPException(_REFUSED.get(exc.code, status.HTTP_409_CONFLICT), str(exc)) from exc
    assert run.id is not None
    pipeline.fire(run.id, slug, admin.username)
    return {"service": slug, **run_to_dict(run)}


@router.get("/dumps")
async def all_dumps(session: SessionDep) -> list[dict[str, Any]]:
    """Every service that has a dump configured: its newest file and how many are kept."""
    out: list[dict[str, Any]] = []
    for svc in session.exec(select(Service).order_by(Service.slug)).all():
        if not svc.dump:
            continue
        rows = records_of(session, svc.slug, 200)
        good = next((r for r in rows if r.verified), None)
        out.append(
            {
                "service": svc.slug,
                "name": svc.name,
                "enabled": bool(svc.dump.get("enabled", True)),
                "type": svc.dump.get("type"),
                "schedule": svc.dump.get("schedule") or "",
                "count": len(rows),
                "total_bytes": sum(r.size_bytes for r in rows),
                "latest": record_view(good) if good else None,
            }
        )
    return out


def _record(session: SessionDep, dump_id: int) -> DumpRecord:
    row = session.get(DumpRecord, dump_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no dump {dump_id}")
    return row


@router.get("/dumps/{dump_id}/download")
async def download(dump_id: int, session: SessionDep, settings: SettingsDep) -> FileResponse:
    row = _record(session, dump_id)
    try:
        path = file_of(settings, row)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "the file is gone from the dump directory")
    return FileResponse(path, media_type="application/gzip", filename=path.name)


@router.post("/dumps/{dump_id}/protect")
async def protect(
    dump_id: int, body: ProtectIn, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    row = _record(session, dump_id)
    row.protected = body.protected
    session.add(row)
    audit.record(
        session,
        actor=admin.username,
        action="dump.protect" if body.protected else "dump.unprotect",
        target=f"service:{row.service_slug}",
        details={"dump_id": row.id, "file": row.path},
    )
    session.commit()
    return record_view(row)


@router.delete("/dumps/{dump_id}")
async def delete(
    dump_id: int, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    row = _record(session, dump_id)
    if row.protected:
        raise HTTPException(status.HTTP_409_CONFLICT, "this dump is protected - unprotect it first")
    view = record_view(row)
    remove_record(session, settings, row)
    audit.record(
        session,
        actor=admin.username,
        action="dump.delete",
        target=f"service:{view['service']}",
        details={"dump_id": dump_id, "file": view["path"]},
    )
    session.commit()
    return view


__all__ = ["router"]
