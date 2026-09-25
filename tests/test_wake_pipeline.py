"""Wake pipeline behaviour, driven entirely through the dry-run adapters."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterUnreachable
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.config import get_settings
from orchestrator.db.models import AuditEntry, Node, NodeState, PipelineRun
from orchestrator.domain.enums import GuestKind, HealthState, PipelineStatus
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.pipelines.wake import WakeError, WakePipeline
from orchestrator.registry.loader import reconcile_yaml_into_db

EXAMPLE = Path(__file__).parent.parent / "config" / "services.example.yaml"

# example-media / compute-a / vm 9001 — a burst node, which is what makes it
# useful here: the wake pipeline has something to actually do.
SLUG = "example-media"
NODE_NAME = "compute-a"
VMID = 9001


@pytest.fixture
def loaded_session(session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    # Several tests below exercise the poll loop for real (a few iterations),
    # so keep the interval tiny rather than the real 5s default.
    from orchestrator import config as config_module

    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    config_module.reset_settings_cache()

    yaml_path = get_settings().services_yaml_path
    shutil.copy(EXAMPLE, yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()
    return session


class StubHealthEngine:
    """Returns a scripted sequence of verdicts without touching the network.

    The last entry repeats for any call past the end of the list, so a test
    can say "stays UNKNOWN forever" with one-element list.
    """

    def __init__(self, states: list[HealthState] | None = None, reason: str = "stub") -> None:
        self._states = states or [HealthState.HEALTHY]
        self._i = 0
        self.reason = reason
        self.scanned: list[str] = []

    async def scan(self, session: Session, service) -> ServiceVerdict:
        self.scanned.append(service.slug)
        state = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return ServiceVerdict(state=state, reason=self.reason, probe_results=[])


class FlakyProxmoxAdapter(DryRunProxmoxAdapter):
    """A node that reports offline (or unreachable) for a fixed number of
    cluster_status() polls, then settles into the given online state."""

    def __init__(
        self,
        *,
        node_name: str,
        offline_polls: int = 0,
        unreachable_polls: int = 0,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._node_name = node_name
        self._offline_polls = offline_polls
        self._unreachable_polls = unreachable_polls
        self.cluster_status_calls = 0

    async def cluster_status(self) -> ClusterStatus:
        self.cluster_status_calls += 1
        if self._unreachable_polls > 0:
            self._unreachable_polls -= 1
            raise AdapterUnreachable("proxmox mid-restart")
        online = self.cluster_status_calls > self._offline_polls
        return ClusterStatus(nodes=[ClusterNode(name=self._node_name, online=online, local=True)])


def _pipeline(
    proxmox: DryRunProxmoxAdapter,
    *,
    power: DryRunPowerAdapter | None = None,
    health: StubHealthEngine | None = None,
) -> tuple[WakePipeline, DryRunPowerAdapter]:
    power = power or DryRunPowerAdapter()
    pipeline = WakePipeline(
        power=power,
        proxmox=proxmox,
        settings=get_settings(),
        health=health or StubHealthEngine(),  # type: ignore[arg-type]
    )
    return pipeline, power


def _stopped_guest() -> Guest:
    return Guest(node=NODE_NAME, vmid=VMID, kind=GuestKind.VM, name="media", status="stopped")


def _running_guest() -> Guest:
    return Guest(node=NODE_NAME, vmid=VMID, kind=GuestKind.VM, name="media", status="running")


async def test_successful_wake_powers_on_starts_the_guest_and_waits_for_health(
    loaded_session: Session,
) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=2, simulated_guests=[_stopped_guest()]
    )
    pipeline, power = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, SLUG)

    assert run.status is PipelineStatus.SUCCEEDED
    assert run.finished_at is not None
    step_names = [s["name"] for s in run.steps]
    assert step_names == [
        "hold_asserted",
        "node_power_on",
        "node_online",
        "guest_start",
        "health_poll",
        "hold_released",
    ]
    assert proxmox.cluster_status_calls == 3  # offline, offline, online

    node = loaded_session.exec(select(Node).where(Node.name == NODE_NAME)).one()
    assert power._simulated_state[node.power_mgr_target].value == "on"


async def test_wake_skips_power_on_when_node_already_online(loaded_session: Session) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=0, simulated_guests=[_running_guest()]
    )
    pipeline, power = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, SLUG)

    assert run.status is PipelineStatus.SUCCEEDED
    steps = {s["name"]: s["status"] for s in run.steps}
    assert steps["node_power"] == "already_on"
    assert steps["guest_start"] == "already_running"
    assert power._simulated_state == {}  # wake() was never called


async def test_wake_tolerates_a_transient_unreachable_while_polling(
    loaded_session: Session,
) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME,
        unreachable_polls=1,
        offline_polls=2,
        simulated_guests=[_running_guest()],
    )
    pipeline, _power = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, SLUG)

    assert run.status is PipelineStatus.SUCCEEDED
    assert proxmox.cluster_status_calls == 3  # unreachable, offline, online


async def test_wake_times_out_when_the_node_never_comes_online(loaded_session: Session) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=10_000, simulated_guests=[_stopped_guest()]
    )
    pipeline, power = _pipeline(proxmox)

    with pytest.raises(WakeError, match="did not come online"):
        await pipeline.run_for_service(loaded_session, SLUG, timeout_s=0)

    run = loaded_session.exec(select(PipelineRun)).one()
    assert run.status is PipelineStatus.FAILED
    assert run.error is not None and "did not come online" in run.error
    node = loaded_session.exec(select(Node).where(Node.name == NODE_NAME)).one()
    assert power._simulated_state.get(node.power_mgr_target) is not None  # wake() was still tried


async def test_wake_times_out_waiting_for_health(loaded_session: Session) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=0, simulated_guests=[_running_guest()]
    )
    health = StubHealthEngine(states=[HealthState.UNKNOWN], reason="probe unreachable")
    pipeline, _power = _pipeline(proxmox, health=health)

    with pytest.raises(WakeError, match="did not become HEALTHY"):
        await pipeline.run_for_service(loaded_session, SLUG, timeout_s=0)

    run = loaded_session.exec(select(PipelineRun)).one()
    assert run.status is PipelineStatus.FAILED
    assert any(s["name"] == "node_online" and s["status"] == "ok" for s in run.steps)


async def test_guest_start_failure_is_a_wake_error_naming_quorum(loaded_session: Session) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=0, simulated_guests=[_stopped_guest()]
    )
    proxmox.next_task_exit_status = "ERROR: no quorum"
    pipeline, _power = _pipeline(proxmox)

    with pytest.raises(WakeError, match="quorate"):
        await pipeline.run_for_service(loaded_session, SLUG)


async def test_hold_is_asserted_and_released_even_on_failure(loaded_session: Session) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=10_000, simulated_guests=[_stopped_guest()]
    )
    pipeline, power = _pipeline(proxmox)

    with pytest.raises(WakeError):
        await pipeline.run_for_service(loaded_session, SLUG, timeout_s=0)

    node = loaded_session.exec(select(Node).where(Node.name == NODE_NAME)).one()
    assert power._holds.get(node.power_mgr_target, set()) == set()  # released, not leaked

    state = loaded_session.exec(select(NodeState).where(NodeState.node_id == node.id)).one()
    assert state.hold_count == 0
    assert state.current_hold_reasons == []


async def test_wake_writes_audit_entries(loaded_session: Session) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=0, simulated_guests=[_running_guest()]
    )
    pipeline, _power = _pipeline(proxmox)

    await pipeline.run_for_service(loaded_session, SLUG)

    actions = {e.action for e in loaded_session.exec(select(AuditEntry)).all()}
    assert {"wake.start", "wake.succeeded"} <= actions


async def test_wake_refuses_an_unknown_slug(loaded_session: Session) -> None:
    proxmox = DryRunProxmoxAdapter()
    pipeline, _power = _pipeline(proxmox)

    with pytest.raises(WakeError, match="no service registered"):
        await pipeline.run_for_service(loaded_session, "does-not-exist")


async def test_wake_refuses_a_disabled_service(loaded_session: Session) -> None:
    from orchestrator.db.models import Service

    service = loaded_session.exec(select(Service).where(Service.slug == SLUG)).one()
    service.enabled = False
    loaded_session.add(service)
    loaded_session.commit()

    proxmox = DryRunProxmoxAdapter()
    pipeline, _power = _pipeline(proxmox)

    with pytest.raises(WakeError, match="disabled"):
        await pipeline.run_for_service(loaded_session, SLUG)


async def test_start_then_execute_produces_the_same_outcome_as_run_for_service(
    loaded_session: Session,
) -> None:
    """This is the split the web layer relies on: start() fast + synchronous,
    execute() does the work against its OWN session."""
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=0, simulated_guests=[_running_guest()]
    )
    pipeline, _power = _pipeline(proxmox)

    run = pipeline.start(loaded_session, SLUG)
    assert run.status is PipelineStatus.RUNNING
    assert run.id is not None

    await pipeline.execute(run.id, SLUG)

    loaded_session.refresh(run)
    assert run.status is PipelineStatus.SUCCEEDED


async def test_execute_never_raises_even_on_failure(loaded_session: Session) -> None:
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=10_000, simulated_guests=[_stopped_guest()]
    )
    pipeline, _power = _pipeline(proxmox)

    run = pipeline.start(loaded_session, SLUG)
    assert run.id is not None

    await pipeline.execute(run.id, SLUG, timeout_s=0)  # must not raise

    loaded_session.refresh(run)
    assert run.status is PipelineStatus.FAILED
