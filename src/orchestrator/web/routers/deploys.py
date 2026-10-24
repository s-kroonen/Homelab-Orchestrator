"""Run a deploy, look at its history. The same pipeline whoever starts it (UI, CLI, webhook).

``POST /api/services/{slug}/deploy`` returns as soon as the run is recorded (a deploy waits for
the stack and the probes, which takes minutes); poll ``GET /api/deploys/{run_id}`` or watch
Activity. An unguarded deploy needs a written reason and is audited.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import desc, select

from orchestrator.db.models import PipelineRun, Service
from orchestrator.deploy.pipeline import DeployPipeline, DeployRefused, Trigger
from orchestrator.domain.enums import PipelineKind
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.wake_support import run_to_dict

router = APIRouter(prefix="/api", tags=["deploys"], dependencies=[Depends(require_admin)])

_REFUSED = {
    "unknown": status.HTTP_404_NOT_FOUND,
    "off": status.HTTP_423_LOCKED,
}


class DeployIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: False = the confirmed override: skip the pre-guards (needs ``reason``).
    guards: bool = True
    #: Run the pre-guards only and report; change nothing.
    guards_only: bool = False
    reason: str = ""


def refusal(exc: DeployRefused) -> HTTPException:
    return HTTPException(_REFUSED.get(exc.code, status.HTTP_409_CONFLICT), str(exc))


def run_view(run: PipelineRun, slug: str | None) -> dict[str, Any]:
    return {"service": slug, **run_to_dict(run)}


@router.post("/services/{slug}/deploy", status_code=status.HTTP_202_ACCEPTED)
async def deploy(
    slug: str,
    body: DeployIn,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    if not body.guards and not body.reason.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "an unguarded deploy needs a reason (it is audited)"
        )
    pipeline = DeployPipeline(adapters, settings)
    trigger = Trigger("manual", admin.username)
    try:
        run = pipeline.start(
            session,
            slug,
            trigger,
            guards=body.guards,
            guards_only=body.guards_only,
            reason=body.reason.strip(),
        )
    except DeployRefused as exc:
        raise refusal(exc) from exc
    assert run.id is not None
    pipeline.fire(run.id, slug, trigger, guards=body.guards, guards_only=body.guards_only)
    return run_view(run, slug)


@router.get("/services/{slug}/deploys")
async def deploy_history(slug: str, session: SessionDep, limit: int = 20) -> list[dict[str, Any]]:
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    runs = session.exec(
        select(PipelineRun)
        .where(PipelineRun.service_id == svc.id, PipelineRun.kind == PipelineKind.DEPLOY)
        .order_by(desc(PipelineRun.started_at))
        .limit(max(1, min(limit, 100)))
    ).all()
    return [run_view(r, slug) for r in runs]


@router.get("/deploys/{run_id}")
async def deploy_run(run_id: int, session: SessionDep) -> dict[str, Any]:
    run = session.get(PipelineRun, run_id)
    if run is None or run.kind is not PipelineKind.DEPLOY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no deploy run {run_id}")
    svc = session.get(Service, run.service_id) if run.service_id else None
    return run_view(run, svc.slug if svc else None)
