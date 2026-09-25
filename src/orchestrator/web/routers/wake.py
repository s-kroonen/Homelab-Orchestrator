"""Wake endpoint.

``POST`` returns as soon as the run is recorded — a cold node can take minutes
to become healthy, and this is meant to be called from Traefik's ``errors``
middleware on behalf of an arbitrary visitor, so it must not block a request
on that. The pipeline itself continues via ``asyncio.create_task`` with its
own DB session, detached from this request.

JSON for now, not HTML: the self-refreshing status page these responses are
meant to back is phase 5's maintenance responder, which will call the same
pipeline. This is the seam it calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlmodel import desc, select

from orchestrator.db.models import PipelineRun, Service
from orchestrator.domain.enums import PipelineKind
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.wake import WakeError, WakePipeline
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

router = APIRouter(tags=["wake"])
log = get_logger(__name__)

# asyncio only holds a weak reference to a task once nothing else does, so a
# fire-and-forget create_task() can be garbage-collected mid-run. Keeping a
# strong reference here until it finishes is the documented workaround.
_background_tasks: set[asyncio.Task[None]] = set()


def _run_to_dict(run: PipelineRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "status": run.status.value,
        "correlation_id": run.correlation_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "steps": run.steps,
    }


@router.post("/wake/{service_slug}", status_code=status.HTTP_202_ACCEPTED)
async def wake(
    service_slug: str, session: SessionDep, adapters: AdaptersDep, settings: SettingsDep
) -> dict[str, Any]:
    pipeline = WakePipeline(power=adapters.power, proxmox=adapters.proxmox, settings=settings)
    try:
        run = pipeline.start(session, service_slug, actor="api")
    except WakeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    run_id = run.id
    assert run_id is not None  # start() always commits and refreshes

    async def _background() -> None:
        try:
            await pipeline.execute(run_id, service_slug, actor="api")
        except Exception:
            log.exception("wake.background_task_crashed", run_id=run_id, service=service_slug)

    task = asyncio.create_task(_background(), name=f"wake:{service_slug}:{run_id}")
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"service": service_slug, **_run_to_dict(run)}


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
    return {"service": service_slug, **_run_to_dict(run)}
