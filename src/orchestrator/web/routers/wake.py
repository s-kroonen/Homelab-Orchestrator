"""Wake endpoint.

``POST`` returns as soon as the run is recorded — a cold node can take minutes
to become healthy, and this is meant to be callable on behalf of an arbitrary
visitor, so it must not block a request on that. The pipeline itself
continues via ``asyncio.create_task`` with its own DB session, detached from
this request.

JSON for now, not HTML: the self-refreshing status page these responses back
is the maintenance responder (``web/routers/maintenance.py``), which calls
the same pipeline through :func:`orchestrator.web.wake_support`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlmodel import desc, select

from orchestrator.db.models import PipelineRun, Service
from orchestrator.domain.enums import PipelineKind
from orchestrator.pipelines.wake import WakeError, WakePipeline
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.wake_support import fire_background, run_to_dict

router = APIRouter(tags=["wake"])


@router.post("/wake/{service_slug}", status_code=status.HTTP_202_ACCEPTED)
async def wake(
    service_slug: str, session: SessionDep, adapters: AdaptersDep, settings: SettingsDep
) -> dict[str, Any]:
    pipeline = WakePipeline(power=adapters.power, proxmox=adapters.proxmox, settings=settings)
    try:
        run, started_new = pipeline.get_or_start(session, service_slug, actor="api")
    except WakeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if started_new:
        fire_background(pipeline, run, service_slug, actor="api")

    return {"service": service_slug, **run_to_dict(run)}


@router.get("/wake/{service_slug}")
async def wake_status(service_slug: str, session: SessionDep) -> dict[str, Any]:
    """The most recent wake run for this service, or idle if there has never been one."""
    service = session.exec(select(Service).where(Service.slug == service_slug)).one_or_none()
    if service is None:
        raise HTTPException(
            status_code=404, detail=f"no service registered with slug {service_slug!r}"
        )

    run = session.exec(
        select(PipelineRun)
        .where(PipelineRun.service_id == service.id, PipelineRun.kind == PipelineKind.WAKE)
        .order_by(desc(PipelineRun.started_at))
        .limit(1)
    ).one_or_none()
    if run is None:
        return {"service": service_slug, "status": "idle"}
    return {"service": service_slug, **run_to_dict(run)}
