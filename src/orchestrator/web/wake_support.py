"""Shared by the ``/wake`` API and the maintenance page — both trigger the
same pipeline and must not double-fire it for the same service.
"""

from __future__ import annotations

import asyncio
from typing import Any

from orchestrator.db.models import PipelineRun
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.wake import WakePipeline

log = get_logger(__name__)

# asyncio only holds a weak reference to a task once nothing else does, so a
# fire-and-forget create_task() can be garbage-collected mid-run. Keeping a
# strong reference here until it finishes is the documented workaround.
_background_tasks: set[asyncio.Task[None]] = set()


def run_to_dict(run: PipelineRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "status": run.status.value,
        "correlation_id": run.correlation_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "steps": run.steps,
    }


def fire_background(pipeline: WakePipeline, run: PipelineRun, slug: str, *, actor: str) -> None:
    """Continue a run :meth:`WakePipeline.start` (or ``get_or_start``) already
    created, detached from the calling request. Call only when that call
    reported ``started_new=True`` — a reused RUNNING run already has its own
    background task in flight."""
    run_id = run.id
    assert run_id is not None  # start() always commits and refreshes

    async def _background() -> None:
        try:
            await pipeline.execute(run_id, slug, actor=actor)
        except Exception:
            log.exception("wake.background_task_crashed", run_id=run_id, service=slug)

    task = asyncio.create_task(_background(), name=f"wake:{slug}:{run_id}")
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
