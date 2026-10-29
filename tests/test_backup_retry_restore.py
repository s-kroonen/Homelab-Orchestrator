"""Backup policy options: wake mode, bounded retries with a notification, and
putting nodes and guests back the way the backup found them."""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.proxmox.base import TaskHandle
from orchestrator.db.models import BackupPolicy, Node, NodeState, PipelineRun
from orchestrator.domain.enums import (
    BackupWakeMode,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.notify import hooks
from orchestrator.pipelines.backup import BackupError
from orchestrator.registry.backup_plan import resolve_backup_plan
from tests import test_backup_wake as base
from tests.test_backup_wake import (
    World,
    WorldPower,
    WorldProxmox,
    _pipeline,
    _scheduler,
    _steps,
    _world,
)

# The fake world's fixtures, reused as they are.
topo = base.topo
_fast_wake = base._fast_wake


class Announcements:
    """Stands in for the notification engine: remembers what was announced."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, session: Session, event: Any) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.event for e in self.events]


@pytest.fixture
def announced() -> Any:
    stub = Announcements()
    hooks.install(stub)  # type: ignore[arg-type]
    yield stub
    hooks.install(None)


class PowerWorld(World):
    """A world where powering a node off really takes it (and its guests) down."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.dead_after_off: set[str] = set()


class OffPower(WorldPower):
    async def power_off(self, node_name: str, *, reason: str, force: bool = False) -> None:
        self.w.events.append(f"power_off:{node_name}")
        self.w.up.discard(node_name)
        for vmid, node in self.w.guest_node.items():
            if node == node_name:
                self.w.running.discard(vmid)


class StopProxmox(WorldProxmox):
    async def stop_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle:
        self.w.events.append(f"stop_guest:{vmid}")
        self.w.running.discard(vmid)
        return await super().stop_guest(node, vmid, kind)


def _pipe(world: World, state: HealthState = HealthState.HEALTHY) -> Any:
    from orchestrator.config import get_settings
    from orchestrator.pipelines.backup import BackupPipeline
    from tests.test_backup_wake import StubHealth, WorldPbs

    return BackupPipeline(
        proxmox=StopProxmox(world),
        pbs=WorldPbs(world),
        settings=get_settings(),
        health=StubHealth(world, state),  # type: ignore[arg-type]
        power=OffPower(world),
    )


# -- wake mode -----------------------------------------------------------------


def test_follow_mode_wakes_a_service_whose_own_wake_on_hit_is_off(
    session: Session, topo: dict[str, Any]
) -> None:
    """A service's switch keeps visitors from waking it; a backup is not a visitor."""
    topo["app"].wake_on_hit_enabled = False
    topo["truenas"].wake_on_hit_enabled = False  # the policy's prerequisite, too
    for row in (topo["app"], topo["truenas"]):
        session.add(row)
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert plan.wake_mode == "follow"
    assert plan.blockers == []


def test_follow_mode_never_blocks_on_an_always_on_node(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["compute"].always_on = True
    topo["compute"].wake_on_hit_enabled = False
    session.add(topo["compute"])
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert plan.blockers == []


def test_follow_mode_blocks_when_the_node_is_switched_off(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["compute"].wake_on_hit_enabled = False
    session.add(topo["compute"])
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert any("node 'compute'" in b for b in plan.blockers)


def test_always_mode_ignores_the_switches(session: Session, topo: dict[str, Any]) -> None:
    topo["app"].wake_on_hit_enabled = False
    topo["compute"].wake_on_hit_enabled = False
    topo["policy"].wake_mode = BackupWakeMode.ALWAYS
    for row in (topo["app"], topo["compute"], topo["policy"]):
        session.add(row)
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert plan.blockers == []
    assert plan.wake_mode == "always"


def test_never_mode_plans_no_wake_at_all(session: Session, topo: dict[str, Any]) -> None:
    topo["policy"].wake_mode = BackupWakeMode.NEVER
    session.add(topo["policy"])
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert plan.steps == [] and plan.will_wake is False


async def test_follow_mode_fails_the_run_at_the_wake_step(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["compute"].wake_on_hit_enabled = False
    session.add(topo["compute"])
    session.commit()
    world = _world(topo)

    with pytest.raises(BackupError, match="wake on hit is switched off for node 'compute'"):
        await _pipeline(world).run_for_service(session, "app", actor="t")

    assert world.count("power_on:compute") == 0


async def test_follow_mode_wakes_a_prerequisite_whose_service_switch_is_off(
    session: Session, topo: dict[str, Any]
) -> None:
    """The storage VM keeps wake on hit off against background clients; its node may be woken.
    The backup wakes it and goes through."""
    topo["truenas"].wake_on_hit_enabled = False
    session.add(topo["truenas"])
    session.commit()
    world = _world(topo)

    run = await _pipeline(world).run_for_service(session, "app", actor="t")

    assert run.status is PipelineStatus.SUCCEEDED
    assert world.count("power_on:storage") == 1


# -- retries and notifications ---------------------------------------------------


async def test_a_blocked_policy_run_is_tried_max_attempts_times_then_gives_up(
    session: Session, topo: dict[str, Any], announced: Announcements
) -> None:
    topo["policy"].max_attempts = 3
    session.add(topo["policy"])
    session.commit()
    world = _world(topo)
    world.power_works = False  # storage never comes up: every attempt is blocked

    outcomes = await _scheduler(world).run_policy("nightly")

    assert {o["backup"] for o in outcomes.values()} == {"failed"}
    assert all(o["attempt"] == 3 and o["retry"] is False for o in outcomes.values())
    session.expire_all()
    runs = session.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.BACKUP)).all()
    assert len(runs) == 9  # three services x three attempts, not infinity
    assert all(r.status is PipelineStatus.FAILED for r in runs)
    kinds = announced.kinds()
    assert kinds.count("backup_retry") == 6  # tries 1 and 2 of each service
    assert kinds.count("backup_failed") == 3  # one give-up per service
    last = [e for e in announced.events if e.event == "backup_failed"][-1]
    assert "after 3 tries" in last.title


async def test_a_definitive_failure_is_not_retried(
    session: Session, topo: dict[str, Any], announced: Announcements
) -> None:
    topo["policy"].max_attempts = 3
    session.add(topo["policy"])
    session.commit()
    world = _world(topo, up={"storage", "compute", "gw"}, running={100, 101, 102, 103})

    outcomes = await _scheduler(world, HealthState.FAILED).run_policy("nightly")

    assert all(o["backup"] == "skipped" and o["retry"] is False for o in outcomes.values())
    session.expire_all()
    runs = session.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.BACKUP)).all()
    assert len(runs) == 3  # FAILED is a verdict, not a blocker: one try each
    assert "backup_retry" not in announced.kinds()


async def test_a_service_that_is_not_healthy_after_boot_is_retried(
    session: Session, topo: dict[str, Any], announced: Announcements
) -> None:
    topo["policy"].max_attempts = 2
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo, up={"compute", "gw"}, running={101, 102, 103})

    outcomes = await _scheduler(world, HealthState.UNKNOWN).run_policy("nightly")

    # UNKNOWN is a skip that is tried again: logged on the first try, announced once at the end
    assert all(o["attempt"] == 2 and o["backup"] == "skipped" for o in outcomes.values())
    assert announced.kinds().count("backup_skipped") == 3
    assert "backup_failed" not in announced.kinds()


# -- putting things back ---------------------------------------------------------


async def test_a_node_the_backup_powered_on_is_powered_off_again(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo)  # compute is off

    run = await _pipe(world).run_for_service(session, "app", actor="t")

    assert run.status is PipelineStatus.SUCCEEDED
    assert world.count("power_on:compute") == 1
    assert world.count("power_off:compute") == 1
    assert world.index("vzdump:101") < world.index("power_off:compute")
    session.refresh(run)
    assert "restore_state:compute" in _steps(run)


async def test_a_node_that_was_already_on_is_left_on(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo, up={"compute"})  # on, but app's guest is stopped

    await _pipe(world).run_for_service(session, "app", actor="t")

    assert world.count("power_off:compute") == 0
    assert world.count("start_guest:101") == 1
    assert world.count("stop_guest:101") == 1  # only what the backup started goes back down


async def test_restore_can_be_turned_off_per_policy(session: Session, topo: dict[str, Any]) -> None:
    topo["policy"].requires = []
    topo["policy"].restore_power_state = False
    session.add(topo["policy"])
    session.commit()
    world = _world(topo)

    await _pipe(world).run_for_service(session, "app", actor="t")

    assert world.count("power_off:compute") == 0 and world.count("stop_guest:101") == 0


async def test_a_failed_backup_still_puts_the_node_back(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo)

    with pytest.raises(BackupError):
        await _pipe(world, HealthState.FAILED).run_for_service(session, "app", actor="t")

    assert world.count("power_off:compute") == 1


async def test_a_node_someone_else_is_holding_is_left_on(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo)
    pipe = _pipe(world)
    node = session.exec(select(Node).where(Node.name == "compute")).one()
    original = pipe._waker.release  # type: ignore[union-attr]

    async def release_then_someone_else_holds(*a: Any, **kw: Any) -> None:
        await original(*a, **kw)
        state = session.get(NodeState, node.id) or NodeState(node_id=node.id)
        state.hold_count = 1
        state.current_hold_reasons = [{"reason": "wake:x"}]
        session.add(state)
        session.commit()

    pipe._waker.release = release_then_someone_else_holds  # type: ignore[union-attr,method-assign]

    run = await pipe.run_for_service(session, "app", actor="t")

    assert world.count("power_off:compute") == 0
    skipped = [s for s in run.steps if s["name"] == "restore_state:compute"]
    assert skipped and skipped[0]["status"] == "skipped"


async def test_a_batch_powers_the_node_off_once_after_the_last_service(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo)
    sched = _scheduler(world)
    sched._pipeline_factory = lambda: _pipe(world)  # type: ignore[method-assign]

    outcomes = await sched.run_policy("nightly")

    assert {o["backup"] for o in outcomes.values()} == {"succeeded"}
    assert world.count("power_on:compute") == 1  # app and db share one boot
    assert world.count("power_off:compute") == 1
    assert world.index("vzdump:102") < world.index("power_off:compute")


def test_policy_fields_round_trip_through_the_loader(session: Session) -> None:
    from orchestrator.registry.schema import BackupPolicySpec

    spec = BackupPolicySpec(name="p", wake_mode="never", max_attempts=5, restore_power_state=False)
    assert (spec.wake_mode, spec.max_attempts, spec.restore_power_state) == (
        BackupWakeMode.NEVER,
        5,
        False,
    )
    row = BackupPolicy(name="x")
    assert row.wake_mode is BackupWakeMode.FOLLOW and row.max_attempts == 3
    assert row.restore_power_state is True
