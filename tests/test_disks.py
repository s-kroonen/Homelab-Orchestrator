"""Guest disk usage: reading it from Proxmox, judging it, alerting, and showing it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator.adapters.errors import AdapterAuthError, AdapterError
from orchestrator.adapters.proxmox.base import Filesystem, Guest, GuestFilesystems
from orchestrator.adapters.proxmox.disks import parse_fsinfo
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import Node, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.health import disks
from orchestrator.notify.links import link_for
from tests.test_notifications import Recorder, engine_with, feed

GB = 1024**3


@pytest.fixture
def session() -> Any:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        yield s


def _vm(vmid: int, status: str = "running", **kw: Any) -> Guest:
    return Guest("node-a", vmid, GuestKind.VM, f"vm-{vmid}", status, **kw)


def _ct(vmid: int, used: int, total: int, status: str = "running") -> Guest:
    return Guest("node-a", vmid, GuestKind.CT, f"ct-{vmid}", status, disk=used, maxdisk=total)


def _fs(mount: str, used_gb: float, total_gb: float) -> Filesystem:
    return Filesystem(mount, "ext4", int(used_gb * GB), int(total_gb * GB))


# -- reading -------------------------------------------------------------------------------------


def test_the_guest_agent_answer_keeps_real_disks_once() -> None:
    payload = {
        "result": [
            {"name": "sda1", "mountpoint": "/", "type": "ext4", "total-bytes": 100, "used-bytes": 90},
            {"name": "sda1", "mountpoint": "/var/lib/docker/x", "type": "ext4",
             "total-bytes": 100, "used-bytes": 90},  # a bind mount of the same disk
            {"name": "sdb", "mountpoint": "/data", "type": "xfs", "total-bytes": 400, "used-bytes": 40},
            {"name": "tmpfs", "mountpoint": "/run", "type": "tmpfs", "total-bytes": 10, "used-bytes": 1},
            {"name": "o", "mountpoint": "/var/lib/docker/overlay2/m", "type": "overlay",
             "total-bytes": 100, "used-bytes": 90},
            {"name": "loop3", "mountpoint": "/snap/core/1", "type": "squashfs",
             "total-bytes": 5, "used-bytes": 5},
            {"name": "sdc", "mountpoint": "/old", "type": "ext4", "total-bytes": 50},  # old agent
            {"name": "C:", "mountpoint": "C:\\", "type": "NTFS", "total-bytes": 200, "used-bytes": 50},
        ]
    }  # fmt: skip
    found = parse_fsinfo(payload)
    assert [(f.mount, f.percent) for f in found] == [("/", 90.0), ("/data", 10.0), ("C:\\", 25.0)]


def test_thresholds_and_room_before_it_counts_as_fine_again() -> None:
    s = get_settings()  # 85 / 95
    assert [disks.classify(p, s) for p in (50, 85, 94.9, 95)] == ["ok", "low", "low", "full"]
    assert disks.classify(83, s, previous="low") == "low"  # within 3 points: stays low
    assert disks.classify(81.9, s, previous="low") == "ok"
    assert disks.classify(93, s, previous="full") == "full"
    assert disks.classify(91, s, previous="full") == "low"


async def test_each_running_guest_is_measured_the_way_it_can_be() -> None:
    px = DryRunProxmoxAdapter(
        [_ct(200, 9 * GB, 10 * GB), _vm(101), _vm(102), _vm(103, status="stopped")]
    )
    px.simulated_filesystems[101] = GuestFilesystems([_fs("/", 2, 10)], source="guest_agent")
    status = await disks.check_disks(px, get_settings())
    by = {g.vmid: g for g in status.guests}
    assert by[200].state == "low" and by[200].source == "lxc"  # from the cluster list itself
    assert by[101].state == "ok" and by[101].worst["percent"] == 20.0
    assert by[102].state == "unmeasured" and "guest agent" in (by[102].error or "")
    assert by[103].state == "off" and by[103].filesystems == []


async def test_the_live_adapter_asks_the_agent_or_the_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.adapters import proxmox_family
    from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter

    asked: list[str] = []

    async def request(self: Any, method: str, path: str, **_kw: Any) -> Any:
        asked.append(path)
        if path.endswith("/qemu/101/agent/get-fsinfo"):
            return {"result": [{"name": "sda", "mountpoint": "/", "type": "ext4",
                                "total-bytes": 10 * GB, "used-bytes": 9 * GB}]}  # fmt: skip
        if path.endswith("/qemu/102/agent/get-fsinfo"):
            raise AdapterError("QEMU guest agent is not running")
        if path.endswith("/qemu/103/agent/get-fsinfo"):
            raise AdapterAuthError("forbidden", status_code=403)
        if path.endswith("/lxc/200/status/current"):
            return {"disk": 1 * GB, "maxdisk": 4 * GB}
        raise AssertionError(path)

    monkeypatch.setattr(proxmox_family.ProxmoxFamilyClient, "request", request)
    adapter = LiveProxmoxAdapter(get_settings())
    vm = await adapter.guest_filesystems("node-a", 101, GuestKind.VM)
    assert vm.source == "guest_agent" and vm.filesystems[0].percent == 90.0
    missing = await adapter.guest_filesystems("node-a", 102, GuestKind.VM)
    assert missing.filesystems == [] and "qemu-guest-agent" in (missing.error or "")
    refused = await adapter.guest_filesystems("node-a", 103, GuestKind.VM)
    assert "VM.GuestAgent.Audit" in (refused.error or "")
    ct = await adapter.guest_filesystems("node-a", 200, GuestKind.CT)
    assert ct.source == "lxc" and ct.filesystems[0].percent == 25.0


# -- alerting ------------------------------------------------------------------------------------


async def test_a_filling_disk_is_a_warning_then_an_error_then_recovers(session: Session) -> None:
    node = Node(name="node-a")
    session.add(node)
    session.commit()
    session.add(Service(slug="web", name="Web", node_id=node.id, guest_kind=GuestKind.VM,
                        guest_id=101, group="apps"))  # fmt: skip
    session.commit()
    px = DryRunProxmoxAdapter([_vm(101)])
    rec = Recorder()
    # Four changes in a row inside the flap window would be "flapping"; here they are
    # milliseconds apart, in life hours.
    engine = engine_with(rec, notify_flap_threshold=10)

    async def cycle(used_gb: float) -> list[Any]:
        px.simulated_filesystems[101] = GuestFilesystems([_fs("/", used_gb, 10)], "guest_agent")
        status = await disks.check_disks(
            px, get_settings(), previous=disks.previous_states(session)
        )
        return (await feed(engine, session, disks.observations(session, status)))[0]

    assert await cycle(5) == []  # first sight
    assert await cycle(9) == []  # one scan is not enough
    (low,) = await cycle(9)
    assert low.event == "disk_low" and low.severity == "warn" and low.level == "disk"
    assert low.title == "Disk on vm-101 is 90% full (/)"
    assert "9.0 GB of 10.0 GB used" in low.body and "threshold is 85%" in low.body
    assert link_for(low.level, low.target, low.event) == "/nodes?q=101"
    await cycle(9.7)
    (full,) = await cycle(9.7)
    assert full.event == "disk_full" and full.severity == "error"
    await cycle(8.3)  # 83%: under 85, but not by enough to call it fine
    await cycle(8.3)
    await cycle(5)
    (back,) = await cycle(5)
    assert back.event == "recovered" and back.title == "Disk on vm-101 recovered"
    assert [t for _, _, t in rec.sent][-1] == "Disk on vm-101 recovered"


async def test_a_guest_that_cannot_be_measured_or_is_off_is_left_alone(session: Session) -> None:
    px = DryRunProxmoxAdapter([_vm(102), _vm(103, status="stopped")])
    status = await disks.check_disks(px, get_settings())
    assert disks.observations(session, status) == []


# -- showing -------------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from orchestrator.web.app import create_app
    from tests.test_proxy_hosts import _login_as_first_admin

    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_the_guest_list_and_the_api_show_the_last_check(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    px = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    guests = [_ct(200, 1 * GB, 10 * GB), _vm(101)]

    async def list_guests(node: str | None = None) -> list[Guest]:
        return guests

    async def guest_filesystems(node: str, vmid: int, kind: GuestKind) -> GuestFilesystems:
        return GuestFilesystems([_fs("/", 9.6, 10)], "guest_agent")

    monkeypatch.setattr(px, "list_guests", list_guests)
    monkeypatch.setattr(px, "guest_filesystems", guest_filesystems)

    before = client.get("/api/infra/proxmox/guests").json()
    assert all(g["disk"] is None for g in before)  # not checked yet
    r = client.post("/api/infra/disks/check")
    assert r.status_code == 200, r.text
    assert [g["vmid"] for g in r.json()["guests"]] == [101, 200]  # fullest first
    rows = {g["vmid"]: g for g in client.get("/api/infra/proxmox/guests").json()}
    assert rows[101]["disk"]["state"] == "full" and rows[101]["disk"]["worst"]["mount"] == "/"
    assert rows[200]["disk"]["state"] == "ok"
    listing = client.get("/api/infra/disks").json()
    assert listing["warn_pct"] == 85 and listing["guests"][0]["state"] == "full"
    js = client.get("/static/nodes.js").text
    assert "Disk filling up" in js and "/api/infra/disks/check" in js


def test_the_assistant_tool_reads_the_same(session: Session) -> None:
    from orchestrator.mcp.tools import TOOLS

    tool = TOOLS["disk_usage"]
    assert tool.scope == "read" and not tool.writes
