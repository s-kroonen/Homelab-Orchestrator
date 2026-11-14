"""Runs nobody will finish never lock a service: a stale RUNNING wake made every
backup that needed it fail at once ("a wake ... started elsewhere is still running
after 600s"), night after night, until someone noticed."""

# ruff: noqa: F811 - imported pytest fixtures are re-declared as test arguments

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.config import get_settings
from orchestrator.db.models import AuditEntry, PipelineRun, Service
from orchestrator.domain.enums import PipelineKind, PipelineStatus
from orchestrator.pipelines.interrupted import INTERRUPTED, close_interrupted_runs
from orchestrator.pipelines.wake import WakePipeline
from tests.test_wake_pipeline import (  # noqa: F401 - loaded_session is a fixture
    NODE_NAME,
    SLUG,
    FlakyProxmoxAdapter,
    StubHealthEngine,
    _running_guest,
    loaded_session,
)


def _service(session: Session) -> Service:
    return session.exec(select(Service).where(Service.slug == SLUG)).one()


def _run(session: Session, kind: PipelineKind, status: PipelineStatus, **kw: object) -> PipelineRun:
    run = PipelineRun(
        kind=kind,
        correlation_id="c0ffee",
        service_id=_service(session).id,
        status=status,
        **kw,  # type: ignore[arg-type]
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _pipeline(health: StubHealthEngine | None = None) -> WakePipeline:
    return WakePipeline(
        power=DryRunPowerAdapter(),
        proxmox=FlakyProxmoxAdapter(node_name=NODE_NAME, simulated_guests=[_running_guest()]),
        settings=get_settings(),
        health=health or StubHealthEngine(),  # type: ignore[arg-type]
    )


# -- at start-up --------------------------------------------------------------------


def test_start_up_closes_every_run_the_previous_process_left_open(
    loaded_session: Session,
) -> None:
    wake = _run(loaded_session, PipelineKind.WAKE, PipelineStatus.RUNNING)
    dump = _run(loaded_session, PipelineKind.DUMP, PipelineStatus.QUEUED)
    done = _run(loaded_session, PipelineKind.BACKUP, PipelineStatus.SUCCEEDED)

    closed = close_interrupted_runs(loaded_session)

    assert sorted(closed) == sorted([wake.id, dump.id])
    for run in (wake, dump):
        loaded_session.refresh(run)
        assert run.status is PipelineStatus.FAILED
        assert run.error == INTERRUPTED
        assert run.finished_at is not None
        assert run.steps[-1]["name"] == "interrupted"
    loaded_session.refresh(done)
    assert done.status is PipelineStatus.SUCCEEDED
    actions = {a.action for a in loaded_session.exec(select(AuditEntry)).all()}
    assert {"wake.interrupted", "dump.interrupted"} <= actions


def test_start_up_with_nothing_open_changes_nothing(loaded_session: Session) -> None:
    _run(loaded_session, PipelineKind.WAKE, PipelineStatus.FAILED)
    assert close_interrupted_runs(loaded_session) == []


# -- a wake marked running far longer than any wake takes ------------------------------


async def test_a_wake_running_longer_than_any_wake_can_is_closed_and_a_new_one_runs(
    loaded_session: Session,
) -> None:
    pipeline = _pipeline()
    old = datetime.now(UTC) - timedelta(seconds=pipeline.stale_after_s() + 60)
    stale = _run(loaded_session, PipelineKind.WAKE, PipelineStatus.RUNNING, started_at=old)

    run = await pipeline.ensure_awake(loaded_session, SLUG)

    assert run.id != stale.id
    assert run.status is PipelineStatus.SUCCEEDED
    loaded_session.refresh(stale)
    assert stale.status is PipelineStatus.FAILED
    assert stale.error is not None and stale.error.startswith("abandoned:")
    assert stale.steps[-1]["name"] == "abandoned"


async def test_a_recent_running_wake_is_still_joined_not_closed(loaded_session: Session) -> None:
    pipeline = _pipeline()
    live = _run(loaded_session, PipelineKind.WAKE, PipelineStatus.RUNNING)

    run, started_new = pipeline.get_or_start(loaded_session, SLUG)

    assert (run.id, started_new) == (live.id, False)
    loaded_session.refresh(live)
    assert live.status is PipelineStatus.RUNNING


# -- an error the pipeline has no branch for --------------------------------------------


class _Boom(StubHealthEngine):
    async def scan(self, session: Session, service: object):  # type: ignore[override]
        raise RuntimeError("database is locked")


async def test_an_unexpected_error_in_a_wake_still_closes_its_run(
    loaded_session: Session,
) -> None:
    pipeline = _pipeline(_Boom())

    with pytest.raises(RuntimeError, match="database is locked"):
        await pipeline.run_for_service(loaded_session, SLUG)

    run = loaded_session.exec(
        select(PipelineRun).where(PipelineRun.kind == PipelineKind.WAKE)
    ).one()
    assert run.status is PipelineStatus.FAILED
    assert run.error is not None and "RuntimeError" in run.error
    names = [s["name"] for s in run.steps]
    assert "aborted" in names
    assert names[-1] == "hold_released"  # the hold is still let go
