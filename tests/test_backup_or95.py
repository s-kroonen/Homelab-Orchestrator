"""OR-95: a backup that powered a node on puts it back, failed or not; a hold a restart
cut off does not keep the node on for good; a full datastore and a failed dump say why."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.pbs.base import DatastoreStatus
from orchestrator.adapters.proxmox.base import TaskHandle, TaskStatus
from orchestrator.config import get_settings
from orchestrator.db.models import AuditEntry, Node, NodeState
from orchestrator.domain.enums import PipelineStatus
from orchestrator.pipelines.backup import BackupError, BackupPipeline, task_error_lines
from orchestrator.pipelines.interrupted import clear_stale_holds
from tests import test_backup_wake as base
from tests.test_backup_retry_restore import OffPower, StopProxmox
from tests.test_backup_retry_restore import announced as announced  # the fixture
from tests.test_backup_wake import StubHealth, World, WorldPbs, _scheduler, _steps, _world

topo = base.topo
_fast_wake = base._fast_wake

DUMP_LOG = [
    "INFO: starting new backup job: vzdump 101 --storage example-pbs-storage",
    "INFO: Starting Backup of VM 101 (qemu)",
    "ERROR: VM 101 qmp command 'backup' failed - backup register image failed: "
    "command error: no space left on device",
    "INFO: Failed at 2026-11-23 02:08:15",
    "ERROR: Backup of VM 101 failed - no space left on device",
    "INFO: Backup job finished with errors",
    "TASK ERROR: job errors",
]


class FailingDump(StopProxmox):
    """Every vzdump ends with "job errors", the way a full datastore made them end."""

    def __init__(self, world: World) -> None:
        super().__init__(world)
        self.dumps: set[str] = set()

    async def vzdump(self, **kwargs: Any) -> TaskHandle:
        handle = await super().vzdump(**kwargs)
        self.dumps.add(handle.upid)
        return handle

    async def wait_for_task(self, node: str, upid: str, **kw: Any) -> TaskStatus:
        if upid in self.dumps:
            return TaskStatus(
                upid=upid, status="stopped", exit_status="job errors", log_tail=DUMP_LOG
            )
        return await super().wait_for_task(node, upid, **kw)


class FullPbs(WorldPbs):
    async def datastore_status(self, name: str) -> DatastoreStatus:
        self.w.events.append("pbs_preflight")
        return DatastoreStatus(
            name=name,
            total_bytes=307 * 1024**3,
            used_bytes=307 * 1024**3,
            available_bytes=0,
            reachable=True,
        )


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "backup_power_off_grace_s", 0)


def _pipe(world: World, *, dump_fails: bool = True, pbs: Any = None) -> BackupPipeline:
    return BackupPipeline(
        proxmox=FailingDump(world) if dump_fails else StopProxmox(world),
        pbs=pbs or WorldPbs(world),
        settings=get_settings(),
        health=StubHealth(world),  # type: ignore[arg-type]
        power=OffPower(world),
    )


def _no_policy_prereqs(session: Session, topo: dict[str, Any]) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()


def _power_actions(session: Session) -> list[AuditEntry]:
    return list(
        session.exec(select(AuditEntry).where(AuditEntry.action == "node.power_action")).all()
    )


# -- a failed backup puts the node back -------------------------------------------


async def test_a_failed_dump_powers_the_node_it_woke_off_again(
    session: Session, topo: dict[str, Any]
) -> None:
    _no_policy_prereqs(session, topo)
    world = _world(topo)  # compute is off

    with pytest.raises(BackupError):
        await _pipe(world).run_for_service(session, "app", actor="t")

    assert world.count("power_on:compute") == 1
    assert world.count("power_off:compute") == 1
    run = session.exec(select(base.PipelineRun).where(base.PipelineRun.kind == "backup")).one()
    assert run.status is PipelineStatus.FAILED
    restore = [s for s in run.steps if s["name"] == "restore_state:compute"]
    assert restore and restore[0]["status"] == "ok"
    (entry,) = _power_actions(session)
    assert entry.actor == "backup" and entry.target == "node:compute"


async def test_a_batch_whose_every_dump_fails_powers_the_storage_node_off(
    session: Session, topo: dict[str, Any]
) -> None:
    """Last night's shape: the policy wakes the storage node, the targets' node is already
    on, every dump fails. The storage node goes back off once, after the last service."""
    world = _world(topo, up={"compute"}, running={101, 102})
    sched = _scheduler(world)
    sched._pipeline_factory = lambda: _pipe(world)  # type: ignore[method-assign]

    outcomes = await sched.run_policy("nightly")

    assert {o["backup"] for o in outcomes.values()} == {"failed"}
    assert world.count("power_on:storage") == 1
    assert world.count("power_off:storage") == 1
    assert world.index("vzdump:103") < world.index("power_off:storage")
    assert [e.target for e in _power_actions(session)] == ["node:storage"]
    # the batch has no run of its own: its put-back is on record in the activity feed
    put_back = session.exec(
        select(AuditEntry).where(AuditEntry.action == "backup.restore_state")
    ).all()
    by_node = {e.target: e.details["status"] for e in put_back}
    assert by_node["node:storage"] == "ok"


# -- a hold the restart cut off ----------------------------------------------------


def _hold(session: Session, name: str, **entry: Any) -> NodeState:
    node = session.exec(select(Node).where(Node.name == name)).one()
    state = session.get(NodeState, node.id) or NodeState(node_id=node.id)
    state.hold_count = 1
    state.current_hold_reasons = [{"handle": "h1", "reason": "wake:truenas", **entry}]
    session.add(state)
    session.commit()
    return state


async def test_an_expired_hold_does_not_keep_the_node_on(
    session: Session, topo: dict[str, Any]
) -> None:
    _no_policy_prereqs(session, topo)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _hold(session, "compute", at=past, until=past)
    world = _world(topo)

    with pytest.raises(BackupError):
        await _pipe(world).run_for_service(session, "app", actor="t")

    assert world.count("power_off:compute") == 1


async def test_a_node_left_on_is_announced_and_on_record(
    session: Session, topo: dict[str, Any], announced: Any
) -> None:
    """A hold from before holds had an expiry (the live one from 11-06): the node stays
    on, but now someone is told."""
    _no_policy_prereqs(session, topo)
    _hold(session, "compute", at=(datetime.now(UTC) - timedelta(days=17)).isoformat())
    world = _world(topo)

    with pytest.raises(BackupError):
        await _pipe(world).run_for_service(session, "app", actor="t")

    assert world.count("power_off:compute") == 0
    run = session.exec(select(base.PipelineRun).where(base.PipelineRun.kind == "backup")).one()
    skipped = [s for s in run.steps if s["name"] == "restore_state:compute"]
    assert skipped[0]["status"] == "skipped"
    assert "wake:truenas" in skipped[0]["detail"]["reason"]
    left_on = [e for e in announced.events if e.event == "power_restore_skipped"]
    assert left_on and "compute was left on" in left_on[0].title


def test_holds_left_by_the_previous_process_are_cleared_at_start_up(
    session: Session, topo: dict[str, Any]
) -> None:
    _hold(session, "storage", at="2026-11-06T03:35:01+00:00")

    cleared = clear_stale_holds(session)

    assert cleared == {"storage": ["wake:truenas"]}
    node = session.exec(select(Node).where(Node.name == "storage")).one()
    state = session.get(NodeState, node.id)
    assert state is not None and state.hold_count == 0 and state.current_hold_reasons == []
    entry = session.exec(select(AuditEntry).where(AuditEntry.action == "node.holds_cleared")).one()
    assert entry.target == "node:storage" and entry.details["hold_count"] == 1
    assert clear_stale_holds(session) == {}  # nothing left the second time


async def test_a_hold_taken_now_carries_its_expiry(session: Session, topo: dict[str, Any]) -> None:
    _no_policy_prereqs(session, topo)
    world = _world(topo)
    pipe = _pipe(world, dump_fails=False)
    seen: list[list[dict[str, Any]]] = []
    original = pipe._waker._wake.release_hold  # type: ignore[union-attr]

    async def spy(session: Session, steps: Any, node: Node, handle: str) -> None:
        state = session.get(NodeState, node.id)
        seen.append(list(state.current_hold_reasons) if state else [])
        await original(session, steps, node, handle)

    pipe._waker._wake.release_hold = spy  # type: ignore[union-attr,method-assign]
    await pipe.run_for_service(session, "app", actor="t")

    assert seen and all("until" in h for held in seen for h in held)


# -- a full datastore, and why a dump failed ---------------------------------------


async def test_a_full_datastore_refuses_the_backup_before_the_dump(
    session: Session, topo: dict[str, Any]
) -> None:
    _no_policy_prereqs(session, topo)
    world = _world(topo)

    with pytest.raises(BackupError, match="datastore 'example-datastore' is full: 0 B free"):
        await _pipe(world, dump_fails=False, pbs=FullPbs(world)).run_for_service(
            session, "app", actor="t"
        )

    assert "vzdump:101" not in world.events
    assert world.count("power_off:compute") == 1  # and the node it woke goes back off
    run = session.exec(select(base.PipelineRun).where(base.PipelineRun.kind == "backup")).one()
    pre = next(s for s in run.steps if s["name"] == "pbs_preflight")
    assert pre["status"] == "failed" and pre["detail"]["available_bytes"] == 0
    assert "garbage collection" in (run.error or "")


async def test_the_free_space_check_can_be_turned_off(
    session: Session, topo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_policy_prereqs(session, topo)
    monkeypatch.setattr(get_settings(), "pbs_min_free_bytes", 0)
    world = _world(topo)

    run = await _pipe(world, dump_fails=False, pbs=FullPbs(world)).run_for_service(
        session, "app", actor="t"
    )

    assert run.status is PipelineStatus.SUCCEEDED


async def test_a_failed_dump_says_what_proxmox_logged(
    session: Session, topo: dict[str, Any]
) -> None:
    _no_policy_prereqs(session, topo)
    world = _world(topo)

    with pytest.raises(BackupError) as caught:
        await _pipe(world).run_for_service(session, "app", actor="t")

    assert "job errors. Proxmox:" in str(caught.value)
    assert "no space left on device" in str(caught.value)
    run = session.exec(select(base.PipelineRun).where(base.PipelineRun.kind == "backup")).one()
    finished = next(s for s in run.steps if s["name"] == "vzdump_finished")
    assert "no space left on device" in finished["detail"]["log"]
    assert "vzdump_finished" in _steps(run)


def test_task_error_lines_prefers_the_error_lines() -> None:
    assert task_error_lines(DUMP_LOG).startswith("ERROR: VM 101 qmp command")
    assert "TASK ERROR: job errors" not in task_error_lines(DUMP_LOG)
    assert task_error_lines(["INFO: a", "INFO: b"]) == "INFO: a | INFO: b"
    assert task_error_lines([]) == ""
