"""Wake on hit edge cases: what traffic may wake (``pipelines.wake.traffic_blocker``), decided
before a run exists, and the pipeline's own guard for wakes that traffic started."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from orchestrator.db.models import Node, NodeState, PipelineRun, ProxyHost, Service, ServiceState
from orchestrator.domain.enums import GuestKind, HealthState, PipelineStatus, PowerState
from orchestrator.pipelines.wake import WakeError, traffic_blocker
from orchestrator.pipelines.wake_on_hit import WAKE_ON_HIT_ACTOR, maybe_wake
from tests import test_wake_pipeline as base
from tests.test_wake_pipeline import NODE_NAME, SLUG, FlakyProxmoxAdapter, _pipeline, _stopped_guest

loaded_session = base.loaded_session


def _node(session: Session) -> Node:
    return session.exec(select(Node).where(Node.name == NODE_NAME)).one()


def _service(session: Session) -> Service:
    return session.exec(select(Service).where(Service.slug == SLUG)).one()


def _set(session: Session, *rows: object) -> None:
    for r in rows:
        session.add(r)
    session.commit()


def test_the_node_switch_only_matters_while_the_node_is_off(loaded_session: Session) -> None:
    node, svc = _node(loaded_session), _service(loaded_session)
    node.wake_on_hit_enabled = False
    _set(loaded_session, node)
    assert "wake on hit is off for that node" in (traffic_blocker(loaded_session, svc) or "")

    _set(loaded_session, NodeState(node_id=node.id, power_state=PowerState.ON))
    assert traffic_blocker(loaded_session, svc) is None  # on: only the guest is started

    node.always_on = True
    _set(loaded_session, node)
    state = loaded_session.get(NodeState, node.id)
    assert state is not None
    state.power_state = PowerState.OFF
    _set(loaded_session, state)
    assert traffic_blocker(loaded_session, svc) is None  # never off


def test_a_node_without_a_power_target_is_blocked_only_while_off(loaded_session: Session) -> None:
    node, svc = _node(loaded_session), _service(loaded_session)
    node.power_mgr_target = ""
    _set(loaded_session, node)
    assert "no power target" in (traffic_blocker(loaded_session, svc) or "")
    _set(loaded_session, NodeState(node_id=node.id, power_state=PowerState.ON))
    assert traffic_blocker(loaded_session, svc) is None


def test_the_service_switch_and_the_registry_always_count(loaded_session: Session) -> None:
    svc = _service(loaded_session)
    svc.wake_on_hit_enabled = False
    _set(loaded_session, svc)
    assert traffic_blocker(loaded_session, svc) == "wake on hit is off for this service"
    svc.wake_on_hit_enabled, svc.enabled = True, False
    _set(loaded_session, svc)
    assert traffic_blocker(loaded_session, svc) == "it is switched off in the registry"


def test_a_sub_service_goes_by_its_roots_node(loaded_session: Session) -> None:
    node, root = _node(loaded_session), _service(loaded_session)
    sub = Service(slug="media-addon", name="Add-on", parent_id=root.id, guest_kind=GuestKind.NONE)
    node.wake_on_hit_enabled = False
    _set(loaded_session, sub, node)
    assert "compute-a" in (traffic_blocker(loaded_session, sub) or "")
    node.wake_on_hit_enabled = True
    _set(loaded_session, node)
    assert traffic_blocker(loaded_session, sub) is None


async def test_a_hit_that_may_not_wake_leaves_no_run_behind(loaded_session: Session) -> None:
    node, svc = _node(loaded_session), _service(loaded_session)
    node.power_mgr_target = ""
    ph = ProxyHost(hostname="media.example.lan", service_id=svc.id, wake_on_hit=True)
    _set(loaded_session, node, ph, ServiceState(service_id=svc.id, last_verdict=HealthState.DOWN))
    pipeline, _ = _pipeline(FlakyProxmoxAdapter(node_name=NODE_NAME, offline_polls=99))

    assert maybe_wake(loaded_session, pipeline, ph, source="test") is None
    assert loaded_session.exec(select(PipelineRun)).all() == []


async def test_a_traffic_wake_never_powers_on_a_node_with_its_switch_off(
    loaded_session: Session,
) -> None:
    """The kept power state said on, so the hit got through; the node turned out to be off."""
    node = _node(loaded_session)
    node.wake_on_hit_enabled = False
    _set(loaded_session, node)
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=1, simulated_guests=[_stopped_guest()]
    )
    pipeline, _ = _pipeline(proxmox)

    with pytest.raises(WakeError, match="wake on hit is off"):
        await pipeline.run_for_service(loaded_session, SLUG, actor=WAKE_ON_HIT_ACTOR)

    run = loaded_session.exec(select(PipelineRun)).one()
    assert run.status is PipelineStatus.FAILED
    assert "wake on hit is off" in (run.error or "")
    assert "node_power_on" not in [s["name"] for s in run.steps]


async def test_a_person_can_still_wake_it(loaded_session: Session) -> None:
    node = _node(loaded_session)
    node.wake_on_hit_enabled = False
    _set(loaded_session, node)
    proxmox = FlakyProxmoxAdapter(
        node_name=NODE_NAME, offline_polls=1, simulated_guests=[_stopped_guest()]
    )
    pipeline, _ = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, SLUG, actor="api")

    assert run.status is PipelineStatus.SUCCEEDED
    assert "node_power_on" in [s["name"] for s in run.steps]
