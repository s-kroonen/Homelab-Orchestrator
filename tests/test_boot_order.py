"""Boot order: guests Proxmox starts at boot are left to it after a wake powers their node on,
the order and timing a wake can expect, and the boot settings on Nodes & guests."""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.proxmox.base import GuestBoot, parse_startup
from orchestrator.db.models import Node, NodeState, Service
from orchestrator.domain.enums import GuestKind, PipelineStatus, PowerState
from orchestrator.registry import boot
from tests import test_wake_pipeline as base
from tests.test_wake_pipeline import (
    NODE_NAME,
    SLUG,
    VMID,
    FlakyProxmoxAdapter,
    _pipeline,
    _stopped_guest,
)

loaded_session = base.loaded_session


class BootingProxmox(FlakyProxmoxAdapter):
    """After the node comes online, the guest starts by itself on the ``autostart_after``-th
    look at the guest list (None: never). Counts explicit starts."""

    def __init__(self, *, autostart_after: int | None, **kw: Any) -> None:
        super().__init__(**kw)
        self.autostart_after = autostart_after
        self.looks = 0
        self.starts = 0

    async def list_guests(self, node: str | None = None):  # type: ignore[override]
        self.looks += 1
        if self.autostart_after is not None and self.looks >= self.autostart_after:
            self.simulated_guests = [
                type(g)(g.node, g.vmid, g.kind, g.name, "running") for g in self.simulated_guests
            ]
        return await super().list_guests(node)

    async def start_guest(self, node: str, vmid: int, kind: GuestKind):  # type: ignore[override]
        self.starts += 1
        return await super().start_guest(node, vmid, kind)


def _names(run: Any) -> list[str]:
    return [s["name"] for s in run.steps]


async def test_a_guest_that_starts_at_boot_is_left_to_proxmox(loaded_session: Session) -> None:
    proxmox = BootingProxmox(
        node_name=NODE_NAME, offline_polls=1, simulated_guests=[_stopped_guest()], autostart_after=4
    )
    proxmox.simulated_boot[VMID] = GuestBoot(onboot=True, order=1, up_s=0)
    pipeline, _ = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, SLUG)

    assert run.status is PipelineStatus.SUCCEEDED
    assert proxmox.starts == 0
    auto = next(s for s in run.steps if s["name"] == "guest_autostart")
    assert auto["status"] == "ok" and auto["detail"]["position"] == 1
    assert "guest_start" not in _names(run)


async def test_a_late_start_at_boot_guest_is_started_anyway(
    loaded_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import config as config_module

    monkeypatch.setenv("WAKE_AUTOSTART_GRACE_S", "0")
    config_module.reset_settings_cache()
    proxmox = BootingProxmox(
        node_name=NODE_NAME,
        offline_polls=1,
        simulated_guests=[_stopped_guest()],
        autostart_after=None,
    )
    proxmox.simulated_boot[VMID] = GuestBoot(onboot=True)
    monkeypatch.setattr(boot, "GUEST_START_S", 0)
    pipeline, _ = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, SLUG)

    assert run.status is PipelineStatus.SUCCEEDED
    assert [s["status"] for s in run.steps if s["name"] == "guest_autostart"] == ["late"]
    assert proxmox.starts == 1


async def test_a_guest_that_does_not_start_at_boot_is_started(loaded_session: Session) -> None:
    proxmox = BootingProxmox(
        node_name=NODE_NAME,
        offline_polls=1,
        simulated_guests=[_stopped_guest()],
        autostart_after=None,
    )
    proxmox.simulated_boot[VMID] = GuestBoot(onboot=False)
    pipeline, _ = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, SLUG)

    assert run.status is PipelineStatus.SUCCEEDED
    assert proxmox.starts == 1
    assert "guest_autostart" not in _names(run)


async def test_a_node_without_power_target_that_is_on_still_gets_its_guest_started(
    loaded_session: Session,
) -> None:
    node = loaded_session.exec(select(Node).where(Node.name == NODE_NAME)).one()
    node.power_mgr_target = ""
    loaded_session.add(node)
    loaded_session.add(NodeState(node_id=node.id, power_state=PowerState.ON))
    loaded_session.commit()
    proxmox = BootingProxmox(
        node_name=NODE_NAME,
        offline_polls=0,
        simulated_guests=[_stopped_guest()],
        autostart_after=None,
    )
    pipeline, _ = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, SLUG)

    assert run.status is PipelineStatus.SUCCEEDED
    assert proxmox.starts == 1


async def test_a_sub_service_wakes_through_its_roots_node(loaded_session: Session) -> None:
    root = loaded_session.exec(select(Service).where(Service.slug == SLUG)).one()
    sub = Service(slug="media-addon", name="Add-on", parent_id=root.id, guest_kind=GuestKind.NONE)
    loaded_session.add(sub)
    loaded_session.commit()
    proxmox = BootingProxmox(
        node_name=NODE_NAME,
        offline_polls=1,
        simulated_guests=[_stopped_guest()],
        autostart_after=None,
    )
    pipeline, _ = _pipeline(proxmox)

    run = await pipeline.run_for_service(loaded_session, "media-addon")

    assert run.status is PipelineStatus.SUCCEEDED
    assert proxmox.starts == 1  # its root's guest
    assert "node_power_on" in _names(run)


# -- the sequence and the estimate ------------------------------------------------------------


def test_startup_strings_round_trip() -> None:
    assert parse_startup("order=2,up=30,down=60") == (2, 30, 60)
    assert parse_startup("up=10,bogus=1,order=x") == (None, 10, None)
    assert GuestBoot(onboot=True, order=1, up_s=30).startup == "order=1,up=30"
    assert GuestBoot().startup == ""


def test_the_sequence_follows_proxmox_order_and_delays(loaded_session: Session) -> None:
    node = loaded_session.exec(select(Node).where(Node.name == NODE_NAME)).one()
    loaded_session.add(NodeState(node_id=node.id, onboot_delay_s=20))
    boot.store(loaded_session, NODE_NAME, 300, GuestBoot(onboot=True))  # no order: last
    boot.store(loaded_session, NODE_NAME, 200, GuestBoot(onboot=True, order=2, up_s=30))
    boot.store(loaded_session, NODE_NAME, 100, GuestBoot(onboot=True, order=1, up_s=10))
    boot.store(loaded_session, NODE_NAME, 400, GuestBoot(onboot=False))
    loaded_session.commit()

    seq = boot.sequence(loaded_session, node)

    assert [s.vmid for s in seq] == [100, 200, 300]
    step = boot.GUEST_START_S
    assert [s.starts_after_s for s in seq] == [20, 20 + step + 10, 20 + 2 * step + 10 + 30]


def test_the_estimate_says_what_a_wake_does(loaded_session: Session) -> None:
    node = loaded_session.exec(select(Node).where(Node.name == NODE_NAME)).one()
    svc = loaded_session.exec(select(Service).where(Service.slug == SLUG)).one()
    boot.store(loaded_session, NODE_NAME, VMID, GuestBoot(onboot=True, order=1))
    loaded_session.commit()

    off = boot.estimate(loaded_session, svc, node, VMID, node_on=False)
    on = boot.estimate(loaded_session, svc, node, VMID, node_on=True)

    assert off.starts_itself is True and off.slot is not None and off.slot.position == 1
    assert off.steps[0]["seconds"] == boot.DEFAULT_NODE_BOOT_S
    assert "by itself" in off.steps[1]["what"]
    assert off.total_s > on.total_s
    assert on.steps[0]["what"].startswith("start the guest")
    assert off.learned is False  # no wake history yet
