"""Bits shared by the wake and backup pipelines: the step recorder and the
backup error type.

Lives in its own module so ``pipelines.backup`` can import ``pipelines.wake``
(a backup now wakes its prerequisites) while ``pipelines.wake`` keeps using the
same recorder — without a circular import.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from sqlmodel import Session

from orchestrator.db.models import PipelineRun
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class BackupError(Exception):
    """Raised when a backup run cannot proceed or did not succeed."""


class Recorder(Protocol):
    """What the pipelines need from a step sink. :class:`StepRecorder` writes to a
    ``PipelineRun``; a batch with no run of its own (a scheduled policy waking its
    shared prerequisites) passes a log-only one."""

    def record(
        self,
        name: str,
        status: str,
        *,
        detail: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> None: ...


class StepRecorder:
    """Accumulates per-step timing into ``PipelineRun.steps``.

    Steps are flushed to the DB as they complete so the dashboard (and a
    restart) can see progress mid-run rather than only at the end.
    """

    def __init__(self, session: Session, run: PipelineRun) -> None:
        self._session = session
        self._run = run

    def record(
        self,
        name: str,
        status: str,
        *,
        detail: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC)
        entry = {
            "name": name,
            "status": status,
            "started_at": (started_at or now).isoformat(),
            "finished_at": now.isoformat(),
            "detail": detail or {},
        }
        # Reassign (not append) so SQLAlchemy sees the JSON column as dirty.
        self._run.steps = [*self._run.steps, entry]
        self._session.add(self._run)
        self._session.commit()
        log.info("pipeline.step", run_id=self._run.id, step=name, status=status, **(detail or {}))
