"""Wake-for-backup: plan resolution, the pipeline's wake phase, and how a
scheduled policy shares its prerequisites across a batch.

The world is fully mocked: a tiny stateful ``World`` records every infrastructure
call in order (``events``), so the tests can assert *sequence* — e.g. that the
storage node was powered on before the PBS preflight, and that a hold is released
only after the dump.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.base import DatastoreStatus
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest, TaskHandle
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.config import get_settings
from orchestrator.db.models import (
    AuditEntry,
    BackupPolicy,
    BackupRecord,
    Node,
    NodeState,
    PipelineRun,
    Service,
)
from orchestrator.domain.enums import GuestKind, HealthState, PipelineKind, PipelineStatus
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.pipelines.backup import BackupError, BackupPipeline
from orchestrator.registry.backup_plan import (
    BLOCKER_NO_POWER_TARGET,
    REASON_POLICY,
    REASON_SERVICE,
    REASON_TARGET,
    resolve_backup_plan,
)
from orchestrator.scheduler import BackupScheduler

# ---------------------------------------------------------------------------
# a tiny stateful fake world
# ---------------------------------------------------------------------------


class World:
    def __init__(self, up: set[str] | None = None, running: set[int] | None = None) -> None:
        self.events: list[str] = []
        #: Proxmox node names currently online.
        self.up: set[str] = set(up or set())
        #: vmids currently running.
        self.running: set[int] = set(running or set())
        #: vmid -> node name, filled by ``build``.
        self.guest_node: dict[int, str] = {}
        self.node_names: list[str] = []
        #: When False a power-on does nothing (the node never comes up).
        self.power_works = True

    def index(self, event: str) -> int:
        return self.events.index(event)

    def count(self, prefix: str) -> int:
        return sum(1 for e in self.events if e.startswith(prefix))


class WorldProxmox(DryRunProxmoxAdapter):
    def __init__(self, world: World) -> None:
        super().__init__()
        self.w = world

    async def cluster_status(self) -> ClusterStatus:
        return ClusterStatus(
            nodes=[ClusterNode(name=n, online=n in self.w.up) for n in self.w.node_names]
        )

    async def list_guests(self, node: str | None = None) -> list[Guest]:
        return [
            Guest(
                node=self.w.guest_node[v], vmid=v, kind=GuestKind.VM, name=str(v), status="running"
            )
            for v in sorted(self.w.running)
            if node is None or self.w.guest_node[v] == node
        ]

    async def start_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle:
        self.w.events.append(f"start_guest:{vmid}")
        self.w.running.add(vmid)
        return await super().start_guest(node, vmid, kind)

    async def vzdump(self, **kwargs: Any) -> TaskHandle:
        self.w.events.append(f"vzdump:{kwargs['vmid']}")
        return await super().vzdump(**kwargs)


class WorldPower(DryRunPowerAdapter):
    def __init__(self, world: World) -> None:
        super().__init__()
        self.w = world
        self.kinds: dict[str, str] = {}

    async def wake(self, node_name: str, *, reason: str) -> None:
        self.w.events.append(f"power_on:{node_name}")
        if self.w.power_works:
            self.w.up.add(node_name)

    async def hold(self, node_name: str, *, reason: str, ttl_s: int) -> str:
        # "backup" holds span the whole run; "wake" holds are the wake pipeline's
        # own short ones. Tell them apart so the tests can assert on the former.
        kind = "wake" if reason.startswith("wake:") else "backup"
        self.w.events.append(f"hold:{node_name}:{kind}")
        handle = await super().hold(node_name, reason=reason, ttl_s=ttl_s)
        self.kinds[handle] = kind
        return handle

    async def release(self, node_name: str, *, handle: str) -> None:
        self.w.events.append(f"release:{node_name}:{self.kinds.get(handle, '?')}")
        await super().release(node_name, handle=handle)


class WorldPbs(DryRunPbsAdapter):
    def __init__(self, world: World) -> None:
        super().__init__(synthesize_snapshots=True)
        self.w = world

    async def datastore_status(self, name: str) -> DatastoreStatus:
        self.w.events.append("pbs_preflight")
        return await super().datastore_status(name)


class StubHealth:
    def __init__(self, world: World, state: HealthState = HealthState.HEALTHY) -> None:
        self.w = world
        self.state = state

    async def scan(self, session: Session, service: Service) -> ServiceVerdict:
        self.w.events.append(f"health:{service.slug}")
        return ServiceVerdict(state=self.state, reason="stub", probe_results=[])


@pytest.fixture(autouse=True)
def _fast_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    # Budget 0: a wake that does not succeed on its first poll fails at once.
    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    monkeypatch.setenv("WAKE_TIMEOUT_S", "0")
    config_module.reset_settings_cache()


def _add_node(session: Session, name: str, *, always_on: bool = False, target: str | None = None):
    node = Node(
        name=name,
        always_on=always_on,
        power_mgr_target=name if target is None else target,
    )
    session.add(node)
    session.commit()
    session.refresh(node)
    return node


def _add_service(session: Session, slug: str, node: Node, vmid: int, **kw: Any) -> Service:
    svc = Service(
        slug=slug, name=slug.title(), node_id=node.id, guest_kind=GuestKind.VM, guest_id=vmid, **kw
    )
    session.add(svc)
    session.commit()
    session.refresh(svc)
    return svc


@pytest.fixture
def topo(session: Session) -> dict[str, Any]:
    """storage (off, wakeable) hosts truenas; compute (off) hosts app + db; gw is always on."""
    storage = _add_node(session, "storage")
    compute = _add_node(session, "compute")
    gw = _add_node(session, "gw", always_on=True)
    truenas = _add_service(
        session,
        "truenas",
        storage,
        100,
        backup_excluded=True,
        backup_excluded_reason="hosts the PBS pool",
    )
    app = _add_service(session, "app", compute, 101)
    db = _add_service(session, "db", compute, 102)
    gwsvc = _add_service(session, "gwsvc", gw, 103)
    policy = BackupPolicy(name="nightly", requires=["truenas"], max_attempts=1)
    session.add(policy)
    session.commit()
    session.refresh(policy)
    for svc in (app, db, gwsvc):
        svc.backup_policy_id = policy.id
        session.add(svc)
    session.commit()
    return {
        "policy": policy,
        "truenas": truenas,
        "app": app,
        "db": db,
        "gwsvc": gwsvc,
        "storage": storage,
        "compute": compute,
        "gw": gw,
    }


def _world(topo: dict[str, Any], **kw: Any) -> World:
    w = World(**kw)
    w.node_names = ["storage", "compute", "gw"]
    w.guest_node = {100: "storage", 101: "compute", 102: "compute", 103: "gw"}
    return w


def _pipeline(world: World, state: HealthState = HealthState.HEALTHY) -> BackupPipeline:
    return BackupPipeline(
        proxmox=WorldProxmox(world),
        pbs=WorldPbs(world),
        settings=get_settings(),
        health=StubHealth(world, state),  # type: ignore[arg-type]
        power=WorldPower(world),
    )


def _steps(run: PipelineRun) -> list[str]:
    return [s["name"] for s in run.steps]


# ---------------------------------------------------------------------------
# plan resolution
# ---------------------------------------------------------------------------


def test_plan_orders_policy_then_service_requires_then_target(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["app"].backup_requires = ["db"]
    session.add(topo["app"])
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert [(s.slug, s.reason) for s in plan.steps] == [
        ("truenas", REASON_POLICY),
        ("db", REASON_SERVICE),
        ("app", REASON_TARGET),
    ]
    assert plan.policy == "nightly"
    assert plan.will_wake is True
    assert plan.blockers == []
    assert plan.steps[0].node == "storage"


def test_plan_deduplicates_keeping_first_position(session: Session, topo: dict[str, Any]) -> None:
    # db is required by the service AND is also the target; truenas appears in both lists.
    topo["db"].backup_requires = ["truenas"]
    session.add(topo["db"])
    session.commit()

    plan = resolve_backup_plan(session, topo["db"], topo["policy"])

    assert [s.slug for s in plan.steps] == ["truenas", "db"]
    assert plan.steps[0].reason == REASON_POLICY  # first slot wins


def test_plan_without_wake_before_backup_omits_the_target(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].wake_before_backup = False
    session.add(topo["policy"])
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert [s.slug for s in plan.steps] == ["truenas"]


def test_plan_without_a_policy_still_wakes_the_target(
    session: Session, topo: dict[str, Any]
) -> None:
    plan = resolve_backup_plan(session, topo["app"], None)
    assert [s.slug for s in plan.steps] == ["app"]
    assert plan.policy is None


def test_plan_flags_a_node_with_no_power_target_as_a_blocker(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["storage"].power_mgr_target = ""
    session.add(topo["storage"])
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert plan.steps[0].blocker == BLOCKER_NO_POWER_TARGET
    assert plan.blockers == [f"truenas: {BLOCKER_NO_POWER_TARGET}"]
    assert plan.steps[1].blocker is None


def test_plan_on_always_on_nodes_is_a_no_op(session: Session, topo: dict[str, Any]) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()

    plan = resolve_backup_plan(session, topo["gwsvc"], topo["policy"])

    assert [s.node_power for s in plan.steps] == ["always_on"]
    assert plan.will_wake is False
    assert plan.blockers == []


def test_plan_reports_last_known_power_state(session: Session, topo: dict[str, Any]) -> None:
    from orchestrator.domain.enums import PowerState

    session.add(NodeState(node_id=topo["storage"].id, power_state=PowerState.ON))
    session.commit()

    plan = resolve_backup_plan(session, topo["app"], topo["policy"])

    assert plan.steps[0].node_power == "on"
    assert plan.steps[1].node_power == "unknown"


# ---------------------------------------------------------------------------
# pipeline: the wake phase
# ---------------------------------------------------------------------------


async def test_prerequisite_is_woken_before_the_pbs_preflight(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)

    run = await _pipeline(world).run_for_service(session, "app")

    assert run.status is PipelineStatus.SUCCEEDED
    ev = world.events
    assert ev.index("power_on:storage") < ev.index("pbs_preflight")
    assert ev.index("start_guest:100") < ev.index("pbs_preflight")
    # the target is woken after its prerequisite, still before the dump
    assert ev.index("power_on:storage") < ev.index("power_on:compute") < ev.index("vzdump:101")
    names = _steps(run)
    assert names.index("wake_prerequisite:truenas") < names.index("pbs_preflight")
    assert names.index("wake_target:app") < names.index("pbs_preflight")
    prereq = next(s for s in run.steps if s["name"] == "wake_prerequisite:truenas")
    assert prereq["status"] == "ok"
    assert prereq["detail"]["reason"] == REASON_POLICY
    assert prereq["detail"]["node"] == "storage"
    assert prereq["detail"]["woke"] is True


async def test_holds_cover_the_whole_dump_and_are_released_after_it(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)

    run = await _pipeline(world).run_for_service(session, "app")

    ev = world.events
    assert ev.index("hold:storage:backup") < ev.index("power_on:storage")
    assert ev.index("vzdump:101") < ev.index("release:storage:backup")
    assert ev.index("vzdump:101") < ev.index("release:compute:backup")
    assert world.count("hold:") == world.count("release:")
    assert world.count("hold:storage:backup") == world.count("hold:compute:backup") == 1
    session.expire_all()
    for node in (topo["storage"], topo["compute"]):
        state = session.get(NodeState, node.id)
        assert state is not None and state.hold_count == 0
        assert state.current_hold_reasons == []
    # release is recorded on the run before it is marked finished
    assert _steps(run).count("hold_released") == 2


async def test_hold_is_released_even_when_the_gate_refuses(
    session: Session, topo: dict[str, Any]
) -> None:
    # Everything is already up, so no wake runs; the gate itself refuses.
    world = _world(topo, up={"storage", "compute"}, running={100, 101})

    with pytest.raises(BackupError, match="integrity gate refused"):
        await _pipeline(world, HealthState.FAILED).run_for_service(session, "app")

    assert world.count("hold:") == world.count("release:")
    assert world.count("hold:storage:backup") == 1
    assert "vzdump:101" not in world.events
    session.expire_all()
    state = session.get(NodeState, topo["storage"].id)
    assert state is not None and state.hold_count == 0


async def test_hold_is_released_when_the_pipeline_crashes_unexpectedly(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    pipeline = _pipeline(world)

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("not a BackupError")

    pipeline._run_gate = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await pipeline.run_for_service(session, "app")

    assert world.count("hold:") == world.count("release:")
    assert world.count("hold:storage:backup") == world.count("hold:compute:backup") == 1


async def test_failed_wake_fails_the_run_before_gate_and_dump(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    world.power_works = False  # the storage node never comes up

    with pytest.raises(BackupError) as err:
        await _pipeline(world).run_for_service(session, "app")

    message = str(err.value)
    assert "truenas" in message and "storage" in message
    assert "last known-good backup is untouched" in message
    # nothing past the wake ran
    assert "pbs_preflight" not in world.events
    assert not any(e.startswith("vzdump") for e in world.events)
    assert not any(e.startswith("health:") for e in world.events if e != "health:truenas")
    session.expire_all()
    run = session.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.BACKUP)).one()
    assert run.status is PipelineStatus.FAILED
    names = _steps(run)
    assert "integrity_gate" not in names and "vzdump_started" not in names
    failed = next(s for s in run.steps if s["name"] == "wake_prerequisite:truenas")
    assert failed["status"] == "failed"
    assert failed["detail"]["node"] == "storage"
    assert session.exec(select(BackupRecord)).all() == []
    actions = [a.action for a in session.exec(select(AuditEntry)).all()]
    assert "backup.failed" in actions
    # the hold taken before the failed wake is still released
    assert world.count("hold:") == world.count("release:")
    assert world.count("hold:storage:backup") == 1


async def test_unwakeable_node_fails_with_the_documented_message(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["storage"].power_mgr_target = ""
    session.add(topo["storage"])
    session.commit()
    world = _world(topo)

    with pytest.raises(BackupError) as err:
        await _pipeline(world).run_for_service(session, "app")

    assert (
        "cannot be woken: no power target configured "
        "(set the node's power target or mark it always on)"
    ) in str(err.value)
    assert not any(e.startswith("power_on") for e in world.events)
    assert world.count("hold:") == 0  # failed before touching anything


async def test_unwakeable_node_that_is_already_up_is_just_up(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["storage"].power_mgr_target = ""
    session.add(topo["storage"])
    session.commit()
    world = _world(topo, up={"storage", "compute"}, running={100, 101})

    run = await _pipeline(world).run_for_service(session, "app")

    assert run.status is PipelineStatus.SUCCEEDED
    prereq = next(s for s in run.steps if s["name"] == "wake_prerequisite:truenas")
    assert prereq["detail"]["already_up"] is True
    assert not any(e.startswith("power_on") for e in world.events)


async def test_wake_false_skips_records_and_audits(session: Session, topo: dict[str, Any]) -> None:
    world = _world(topo, up={"storage", "compute"}, running={100, 101})

    run = await _pipeline(world).run_for_service(session, "app", wake=False)

    skipped = next(s for s in run.steps if s["name"] == "wake_prerequisites")
    assert skipped["status"] == "skipped"
    assert skipped["detail"]["would_wake"] == ["truenas", "app"]
    assert world.count("hold:") == 0 and world.count("power_on") == 0
    actions = [a.action for a in session.exec(select(AuditEntry)).all()]
    assert "backup.wake_skipped" in actions
    assert run.status is PipelineStatus.SUCCEEDED


async def test_service_extras_are_woken_between_policy_prereqs_and_target(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["app"].backup_requires = ["db"]
    session.add(topo["app"])
    session.commit()
    world = _world(topo)

    run = await _pipeline(world).run_for_service(session, "app")

    names = [n for n in _steps(run) if n.startswith("wake_")]
    assert names == ["wake_prerequisite:truenas", "wake_prerequisite:db", "wake_target:app"]
    db_step = next(s for s in run.steps if s["name"] == "wake_prerequisite:db")
    assert db_step["detail"]["reason"] == REASON_SERVICE
    assert world.count("hold:compute:backup") == 1  # one hold per node, not per service


async def test_target_that_is_already_up_with_no_prerequisites_changes_nothing(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo, up={"compute"}, running={101})

    run = await _pipeline(world).run_for_service(session, "app")

    assert _steps(run) == [
        "pbs_preflight",
        "integrity_gate",
        "vzdump_started",
        "vzdump_finished",
        "snapshot_located",
        "backup_recorded",
        "pbs_verify",
    ]
    assert world.count("hold:") == 0 and world.count("power_on") == 0


async def test_always_on_target_with_no_prerequisites_changes_nothing(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo, up={"gw"}, running={103})

    run = await _pipeline(world).run_for_service(session, "gwsvc")

    assert not any(n.startswith("wake_") or n.startswith("hold_") for n in _steps(run))


async def test_prerequisites_already_up_are_held_but_not_woken(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo, up={"storage", "compute"}, running={100, 101})

    run = await _pipeline(world).run_for_service(session, "app")

    assert world.count("power_on") == 0
    assert world.count("hold:storage:backup") == 1  # still pinned for the whole dump
    prereq = next(s for s in run.steps if s["name"] == "wake_prerequisite:truenas")
    assert prereq["detail"]["already_up"] is True
    assert run.status is PipelineStatus.SUCCEEDED


async def test_joins_a_wake_already_in_flight_instead_of_double_firing(
    session: Session, topo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visitor (or another backup) is already waking truenas: the backup waits
    for THAT run rather than firing a second power-on."""
    monkeypatch.setenv("WAKE_TIMEOUT_S", "5")
    config_module.reset_settings_cache()
    world = _world(topo, up={"compute"}, running={101})
    other = PipelineRun(
        kind=PipelineKind.WAKE,
        correlation_id="visitor",
        service_id=topo["truenas"].id,
        status=PipelineStatus.RUNNING,
    )
    session.add(other)
    session.commit()
    other_id = other.id

    async def finish_it() -> None:
        await asyncio.sleep(0.02)
        world.up.add("storage")
        world.running.add(100)
        from orchestrator.db.session import get_engine

        with Session(get_engine()) as s:
            row = s.get(PipelineRun, other_id)
            assert row is not None
            row.status = PipelineStatus.SUCCEEDED
            s.add(row)
            s.commit()

    finisher = asyncio.create_task(finish_it())
    run = await _pipeline(world).run_for_service(session, "app")
    await finisher

    assert run.status is PipelineStatus.SUCCEEDED
    assert "power_on:storage" not in world.events
    prereq = next(s for s in run.steps if s["name"] == "wake_prerequisite:truenas")
    assert prereq["detail"]["wake_run_id"] == other_id


async def test_bare_pipeline_without_a_power_adapter_behaves_as_before(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    pipeline = BackupPipeline(
        proxmox=WorldProxmox(world),
        pbs=WorldPbs(world),
        settings=get_settings(),
        health=StubHealth(world),  # type: ignore[arg-type]
    )

    run = await pipeline.run_for_service(session, "app")

    assert run.status is PipelineStatus.SUCCEEDED
    assert not any(n.startswith("wake_") for n in _steps(run))
    assert world.count("power_on") == 0


# ---------------------------------------------------------------------------
# scheduler: policy prerequisites are woken once per policy run
# ---------------------------------------------------------------------------


def _scheduler(world: World, state: HealthState = HealthState.HEALTHY) -> BackupScheduler:
    adapters = AdapterBundle(
        power=WorldPower(world),
        proxmox=WorldProxmox(world),
        pbs=WorldPbs(world),
        proxy=DryRunProxyAdapter(),
        notifier=NullNotifier(),
    )
    settings = get_settings()
    return BackupScheduler(
        adapters=adapters,
        settings=settings,
        pipeline_factory=lambda: BackupPipeline(
            proxmox=adapters.proxmox,
            pbs=adapters.pbs,
            settings=settings,
            health=StubHealth(world, state),  # type: ignore[arg-type]
            power=adapters.power,
        ),
    )


async def test_policy_prerequisites_are_woken_once_for_the_whole_batch(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)

    outcomes = await _scheduler(world).run_policy("nightly")

    assert {s: o["backup"] for s, o in outcomes.items()} == {
        "app": "succeeded",
        "db": "succeeded",
        "gwsvc": "succeeded",
    }
    assert world.count("power_on:storage") == 1  # once, not three times
    assert world.count("hold:storage:backup") == 1
    assert world.count("release:storage:backup") == 1
    # held until the LAST dump finished
    last_dump = max(i for i, e in enumerate(world.events) if e.startswith("vzdump:"))
    assert world.index("release:storage:backup") > last_dump
    # ... and woken before the first PBS preflight
    assert world.index("power_on:storage") < world.index("pbs_preflight")
    session.expire_all()
    runs = session.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.BACKUP)).all()
    assert len(runs) == 3
    for run in runs:
        step = next(s for s in run.steps if s["name"] == "wake_prerequisite:truenas")
        assert step["status"] == "ok" and step["detail"]["shared"] is True
    state = session.get(NodeState, topo["storage"].id)
    assert state is not None and state.hold_count == 0


async def test_failed_policy_prerequisite_fails_every_service_without_rewaking(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    world.power_works = False

    outcomes = await _scheduler(world).run_policy("nightly")

    assert all(o["backup"] == "failed" for o in outcomes.values())
    assert all("truenas" in o["error"] for o in outcomes.values())
    assert world.count("power_on:storage") == 1  # the failed wake is not retried per service
    assert "pbs_preflight" not in world.events
    assert world.count("hold:") == world.count("release:")
    session.expire_all()
    runs = session.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.BACKUP)).all()
    assert [r.status for r in runs] == [PipelineStatus.FAILED] * 3


async def test_one_service_failing_does_not_stop_the_rest_of_the_batch(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["db"].backup_requires = ["gwsvc"]  # fine ...
    topo["app"].backup_requires = ["db"]
    session.add(topo["app"])
    session.add(topo["db"])
    session.commit()
    world = _world(topo)
    # compute never comes up: app and db (both on compute) fail, gwsvc does not need it
    original = WorldPower.wake

    async def selective(self: WorldPower, node_name: str, *, reason: str) -> None:
        if node_name == "compute":
            self.w.events.append("power_on:compute")
            return
        await original(self, node_name, reason=reason)

    WorldPower.wake = selective  # type: ignore[method-assign]
    try:
        outcomes = await _scheduler(world).run_policy("nightly")
    finally:
        WorldPower.wake = original  # type: ignore[method-assign]

    assert outcomes["app"]["backup"] == "failed"
    assert outcomes["db"]["backup"] == "failed"
    assert outcomes["gwsvc"]["backup"] == "succeeded"
    assert world.count("power_on:storage") == 1
    assert world.count("hold:") == world.count("release:")


async def test_scheduler_without_prerequisites_does_not_touch_power(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].requires = []
    session.add(topo["policy"])
    session.commit()
    world = _world(topo, up={"compute", "gw"}, running={101, 102, 103})

    outcomes = await _scheduler(world).run_policy("nightly")

    assert all(o["backup"] == "succeeded" for o in outcomes.values())
    assert world.count("power_on") == 0 and world.count("hold:") == 0
