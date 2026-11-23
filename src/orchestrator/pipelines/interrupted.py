"""Runs nobody will ever finish.

Every pipeline (wake, backup, restore, switch, deploy, dump, format) runs inside
the one orchestrator process, and its ``RUNNING`` row doubles as a lock: a second
wake joins it, a backup waits on it, a dump or deploy refuses to start beside it.
A row left ``RUNNING`` by a process that is gone (a restart, an update, a crash)
or by an error the pipeline did not expect therefore blocks that service until a
person notices. Two guards close such rows instead:

* :func:`close_interrupted_runs` - at start-up, before the scheduler, every run
  still ``RUNNING`` or ``QUEUED`` belongs to the previous process: it is closed as
  FAILED with the reason on the run and in the audit log.
* :func:`clear_stale_holds` - at start-up too: a hold a wake or backup put on a node is
  released by that run, so one still counted belongs to a run the restart cut off. Left
  in place it reads as "other work still holds it" for good, and a backup never powers
  the node it woke off again (OR-95).
* :func:`fail_unexpected` - a pipeline that hits an error it has no branch for
  (a bug, a database error, a cancelled task) closes its own run on the way out.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, col, select

from orchestrator.audit import log as audit
from orchestrator.db.models import Node, NodeState, PipelineRun, Service
from orchestrator.domain.enums import AuditResult, PipelineStatus
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

INTERRUPTED = (
    "interrupted: the orchestrator restarted while this run was in progress, so it never "
    "finished. Nothing was retried; start it again if it is still needed."
)


def _target(session: Session, run: PipelineRun) -> str:
    svc = session.get(Service, run.service_id) if run.service_id is not None else None
    return f"service:{svc.slug}" if svc is not None else f"run:{run.id}"


def close_interrupted_runs(session: Session) -> list[int]:
    """Close every run a previous process left RUNNING or QUEUED. Returns their ids.

    Call it once at start-up, before anything can start a new run."""
    stale = session.exec(
        select(PipelineRun).where(
            col(PipelineRun.status).in_([PipelineStatus.RUNNING, PipelineStatus.QUEUED])
        )
    ).all()
    closed: list[int] = []
    now = datetime.now(UTC)
    for run in stale:
        if run.id is None:
            continue
        run.steps = [
            *run.steps,
            {
                "name": "interrupted",
                "status": "failed",
                "started_at": now.isoformat(),
                "finished_at": now.isoformat(),
                "detail": {"reason": "the orchestrator restarted", "was": run.status.value},
            },
        ]
        run.status = PipelineStatus.FAILED
        run.finished_at = now
        run.error = INTERRUPTED
        session.add(run)
        audit.record(
            session,
            actor="system",
            action=f"{run.kind.value}.interrupted",
            target=_target(session, run),
            result=AuditResult.ERROR,
            correlation_id=run.correlation_id,
            details={"run_id": run.id, "error": INTERRUPTED},
        )
        closed.append(run.id)
    if closed:
        session.commit()
        log.warning(
            "pipeline.interrupted_runs_closed",
            runs=closed,
            impact="These runs were cut off by the restart and are marked failed.",
        )
    return closed


def clear_stale_holds(session: Session) -> dict[str, list[str]]:
    """Drop every hold counted on a node by the previous process. Returns node name ->
    the reasons dropped.

    Call it once at start-up, before anything can take a new hold. The power manager's
    own copy of a hold has a TTL and lapses by itself; this is the orchestrator's count."""
    cleared: dict[str, list[str]] = {}
    for state in session.exec(select(NodeState)).all():
        if state.hold_count <= 0 and not state.current_hold_reasons:
            continue
        node = session.get(Node, state.node_id)
        name = node.name if node is not None else f"node {state.node_id}"
        reasons = [str(r.get("reason", "?")) for r in state.current_hold_reasons or []]
        cleared[name] = reasons or [f"hold_count={state.hold_count}"]
        audit.record(
            session,
            actor="system",
            action="node.holds_cleared",
            target=f"node:{name}",
            details={
                "hold_count": state.hold_count,
                "holds": state.current_hold_reasons or [],
                "why": "the orchestrator restarted; the runs that took these holds are gone",
            },
        )
        state.hold_count = 0
        state.current_hold_reasons = []
        session.add(state)
    if cleared:
        session.commit()
        log.warning(
            "pipeline.stale_holds_cleared",
            nodes=cleared,
            impact="Holds left by runs the restart cut off no longer keep these nodes on.",
        )
    return cleared


def fail_unexpected(
    session: Session, run: PipelineRun, exc: BaseException, *, actor: str, action: str
) -> None:
    """Close ``run`` as FAILED after an error its pipeline has no branch for.

    Never raises: the caller re-raises the original error, and a broken database
    must not hide it. The start-up sweep catches what this cannot write."""
    error = f"stopped by an unexpected error ({type(exc).__name__}): {exc}"[:2000]
    try:
        session.rollback()  # a database error leaves the transaction unusable
        now = datetime.now(UTC)
        run.steps = [
            *run.steps,
            {
                "name": "aborted",
                "status": "failed",
                "started_at": now.isoformat(),
                "finished_at": now.isoformat(),
                "detail": {"error": str(exc)[:1000], "type": type(exc).__name__},
            },
        ]
        run.status = PipelineStatus.FAILED
        run.finished_at = now
        run.error = error
        session.add(run)
        audit.record(
            session,
            actor=actor,
            action=action,
            target=_target(session, run),
            result=AuditResult.ERROR,
            correlation_id=run.correlation_id,
            details={"run_id": run.id, "error": error, "unexpected": True},
        )
        session.commit()
    except Exception as write_exc:  # pragma: no cover - the database itself is gone
        log.error(
            "pipeline.close_failed",
            run_id=run.id,
            error=str(write_exc),
            impact="The run stays marked running until the next restart closes it.",
        )
