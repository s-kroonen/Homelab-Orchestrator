"""The QEMU guest agent, read-only: what may be sent, what a guest's reading says, how it is
kept, alerted on, used for IP addresses and shown (docs/guest_agent.md)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator.adapters.errors import AdapterAuthError, AdapterError
from orchestrator.adapters.proxmox.agent import (
    AGENT_READS,
    DANGEROUS_COMMANDS,
    AgentCallRefused,
    agent_method,
)
from orchestrator.adapters.proxmox.base import Guest
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import Node, Service, ServiceState, TargetState
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.guests import checks, setup_guide, store
from orchestrator.guests import view as guest_view
from orchestrator.guests.collector import GuestCollector
from orchestrator.guests.model import AgentState, GuestFacts, GuestRef, GuestTarget
from orchestrator.guests.providers.proxmox import ProxmoxGuestProvider
from orchestrator.health import disks

MAC = "bc:24:11:00:01:05"


@pytest.fixture
def session() -> Any:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        yield s


def _target(vmid: int, *, running: bool = True, kind: str = "vm") -> GuestTarget:
    return GuestTarget(GuestRef("", f"node-a/{vmid}"), f"vm-{vmid}", running, kind, "node-a", vmid)


def _answering(px: DryRunProxmoxAdapter, vmid: int, **over: Any) -> None:
    px.simulated_configs[vmid] = {"agent": "1", "net0": f"virtio={MAC.upper()},bridge=vmbr0",
                                  "ostype": "l26"}  # fmt: skip
    answers: dict[str, Any] = {
        "ping": {},
        "info": {
            "version": "7.2.0",
            "supported_commands": [
                {"name": "guest-ping", "enabled": True},
                {"name": "guest-exec", "enabled": False},
                {"name": "guest-file-write", "enabled": False},
            ],
        },
        "network-get-interfaces": [
            {
                "name": "lo",
                "hardware-address": "00:00:00:00:00:00",
                "ip-addresses": [{"ip-address": "127.0.0.1", "prefix": 8}],
            },
            {
                "name": "docker0",
                "hardware-address": "02:42:ac:11:00:01",
                "ip-addresses": [{"ip-address": "172.17.0.1", "prefix": 16}],
            },
            {
                "name": "eth0",
                "hardware-address": MAC,
                "ip-addresses": [
                    {"ip-address": "10.0.2.45", "prefix": 24},
                    {"ip-address": "fe80::1", "prefix": 64},
                ],
                "statistics": {"rx-bytes": 1000, "tx-bytes": 2000},
            },
        ],
        "get-users": [
            {"user": "admin", "login-time": time.time() - 720},
            {"user": "backup", "login-time": time.time() - 3 * 86400 - 4 * 3600},
        ],
        "fsfreeze-status": "thawed",
        "get-fsinfo": [
            {
                "name": "sda1",
                "mountpoint": "/",
                "type": "ext4",
                "total-bytes": 100,
                "used-bytes": 40,
            },
        ],
        "get-osinfo": {"name": "Debian GNU/Linux", "version-id": "12", "kernel-release": "6.1.0"},
    }
    answers.update(over)
    px.simulated_agent[vmid] = answers


async def _read(px: DryRunProxmoxAdapter, vmid: int, **kw: Any) -> GuestFacts:
    return await ProxmoxGuestProvider(px).read(_target(vmid, **kw), None, os_due=True)


# -- what may be sent ------------------------------------------------------------------------------


def test_only_read_commands_are_allowed() -> None:
    assert AGENT_READS == {
        "ping": "POST",
        "info": "GET",
        "network-get-interfaces": "GET",
        "get-users": "GET",
        "fsfreeze-status": "POST",
        "get-fsinfo": "GET",
        "get-osinfo": "GET",
    }
    for command in ("exec", "exec-status", "file-read", "file-write", "set-user-password",
                    "fsfreeze-freeze", "fsfreeze-thaw", "fstrim", "shutdown", "suspend-disk"):  # fmt: skip
        with pytest.raises(AgentCallRefused):
            agent_method(command)


async def test_the_live_adapter_refuses_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.adapters import proxmox_family
    from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter

    sent: list[tuple[str, str]] = []

    async def request(self: Any, method: str, path: str, **_kw: Any) -> Any:
        sent.append((method, path))
        return {"result": {}}

    monkeypatch.setattr(proxmox_family.ProxmoxFamilyClient, "request", request)
    adapter = LiveProxmoxAdapter(get_settings())
    with pytest.raises(AgentCallRefused):
        await adapter.agent_read("node-a", 101, "exec")
    assert sent == []
    assert await adapter.agent_read("node-a", 101, "ping") == {}
    assert sent == [("POST", "/nodes/node-a/qemu/101/agent/ping")]


async def test_the_dry_run_adapter_keeps_the_same_list() -> None:
    with pytest.raises(AgentCallRefused):
        await DryRunProxmoxAdapter().agent_read("node-a", 101, "file-write")


def test_the_lock_down_steps_block_every_dangerous_command() -> None:
    assert set(setup_guide.BLOCK_RPCS.split(",")) >= DANGEROUS_COMMANDS


# -- reading a guest -------------------------------------------------------------------------------


async def test_an_answering_vm_gives_addresses_sign_ins_disks_and_more() -> None:
    px = DryRunProxmoxAdapter()
    _answering(px, 105)
    f = await _read(px, 105)
    assert f.agent is AgentState.ANSWERS and f.ping_ms is not None
    assert f.addresses == ["10.0.2.45"] and f.address_source == "guest_agent"  # own NIC only
    assert [i.name for i in f.interfaces or []] == ["docker0", "eth0"]
    eth0 = next(i for i in f.interfaces or [] if i.name == "eth0")
    assert eth0.addresses == ["10.0.2.45/24"] and (eth0.rx_bytes, eth0.tx_bytes) == (1000, 2000)
    assert [s.user for s in f.sessions or []] == ["backup", "admin"]  # earliest first
    backup = (f.sessions or [])[0]
    assert 3 * 86400 + 4 * 3600 - 60 <= backup.seconds() <= 3 * 86400 + 4 * 3600 + 60
    assert f.freeze == "thawed" and f.filesystems == [
        {"mount": "/", "fstype": "ext4", "used": 40, "total": 100}
    ]
    assert (f.os_name, f.os_version, f.kernel) == ("Debian GNU/Linux", "12", "6.1.0")
    assert f.locked_down is True and f.agent_version == "7.2.0"
    assert f.freeze_on_backup is True
    assert {c for _, _, c in px.agent_calls} <= set(AGENT_READS)


async def test_the_reasons_an_agent_cannot_be_read() -> None:
    px = DryRunProxmoxAdapter()
    px.simulated_configs[121] = {"ostype": "l26"}  # option off
    px.simulated_configs[114] = {"agent": "1"}
    px.simulated_agent[114] = {"ping": AdapterAuthError("forbidden", status_code=403)}
    px.simulated_configs[112] = {"agent": "enabled=1,freeze-fs-on-backup=0"}  # nothing answers

    off = await _read(px, 121)
    assert off.agent is AgentState.NOT_ENABLED and "option is off" in off.agent_detail
    refused = await _read(px, 114)
    assert refused.agent is AgentState.NO_PERMISSION
    assert "VM.GuestAgent.Audit" in refused.agent_detail and "/vms/114" in refused.agent_detail
    silent = await _read(px, 112)
    assert silent.agent is AgentState.NOT_ANSWERING and silent.misses == 1
    assert silent.freeze_on_backup is False
    stopped = await _read(px, 105, running=False)
    assert stopped.agent is AgentState.NOT_RUNNING
    # nothing past ping was asked of the guests that could not be read
    assert {(v, c) for _, v, c in px.agent_calls} == {(114, "ping"), (112, "ping")}


async def test_one_failing_command_does_not_lose_the_others() -> None:
    px = DryRunProxmoxAdapter()
    _answering(px, 105, **{"get-users": AdapterError("not supported")})
    f = await _read(px, 105)
    assert f.agent is AgentState.ANSWERS and f.sessions is None
    assert "get-users" in f.errors and f.addresses == ["10.0.2.45"]


async def test_a_container_needs_no_agent() -> None:
    from orchestrator.adapters.proxmox.base import GuestNetwork

    px = DryRunProxmoxAdapter()
    px.simulated_networks[130] = GuestNetwork(macs=[MAC], ips=["10.0.2.30"], source="lxc")
    f = await _read(px, 130, kind="ct")
    assert f.agent is AgentState.NOT_NEEDED and f.addresses == ["10.0.2.30"]
    assert px.agent_calls == []


def test_a_reading_survives_storage_unchanged() -> None:
    import asyncio

    px = DryRunProxmoxAdapter()
    _answering(px, 105)
    f = asyncio.run(_read(px, 105))
    assert GuestFacts.from_dict(f.as_dict()).as_dict() == f.as_dict()


# -- reading them all ------------------------------------------------------------------------------


async def test_a_guest_that_keeps_not_answering_is_read_less_often() -> None:
    px = DryRunProxmoxAdapter()
    px.simulated_configs[112] = {"agent": "1"}
    collector = GuestCollector(ProxmoxGuestProvider(px))
    t = _target(112)
    prev = await collector.read_one(t, None)
    prev = (await collector.collect([t], {t.ref: prev}))[0]
    assert prev.misses == 2 and len(px.agent_calls) == 2
    prev = (await collector.collect([t], {t.ref: prev}))[0]  # skipped
    prev = (await collector.collect([t], {t.ref: prev}))[0]  # skipped
    assert len(px.agent_calls) == 2 and prev.skipped == 2
    prev = (await collector.collect([t], {t.ref: prev}))[0]  # read again
    assert len(px.agent_calls) == 3 and prev.skipped == 0
    await collector.read_one(t, prev)  # Read now ignores the back-off
    assert len(px.agent_calls) == 4


async def test_the_os_is_read_once_a_day() -> None:
    px = DryRunProxmoxAdapter()
    _answering(px, 105)
    collector = GuestCollector(ProxmoxGuestProvider(px))
    t = _target(105)
    first = (await collector.collect([t], {}))[0]
    again = (await collector.collect([t], {t.ref: first}))[0]
    assert [c for _, _, c in px.agent_calls].count("get-osinfo") == 1
    assert again.os_name == "Debian GNU/Linux"


def test_readings_are_kept_and_forgotten_with_their_guest(session: Session) -> None:
    a = GuestFacts(
        ref=GuestRef("pve", "node-a/101"), name="a", read_at=datetime.now(UTC).isoformat()
    )
    b = GuestFacts(ref=GuestRef("pve", "node-a/102"), name="b")
    store.save_many(session, [a, b])
    session.commit()
    assert store.load(session, a.ref).name == "a"  # type: ignore[union-attr]
    assert store.load_by_key(session, "node-a/102").name == "b"  # type: ignore[union-attr]
    assert store.forget_missing(session, "pve", {"node-a/101"}) == 1
    session.commit()
    assert set(store.by_key(session)) == {"node-a/101"}


# -- alerts ----------------------------------------------------------------------------------------


def _facts(**kw: Any) -> GuestFacts:
    base: dict[str, Any] = {"ref": GuestRef("", "node-a/105"), "name": "media", "running": True}
    base.update(kw)
    return GuestFacts(**base)


def test_the_agent_checks_say_only_what_they_know() -> None:
    scope = checks.Scope(node="node-a", slug="media")
    answers, lockdown = checks.AnswersCheck(), checks.LockdownCheck()
    ok = _facts(agent=AgentState.ANSWERS, enabled_commands=["guest-ping"])
    assert answers.observe(ok, scope).state == "healthy"  # type: ignore[union-attr]
    assert lockdown.observe(ok, scope).state == "healthy"  # type: ignore[union-attr]
    silent = _facts(agent=AgentState.NOT_ANSWERING, agent_detail="timed out")
    obs = answers.observe(silent, scope)
    assert obs is not None and obs.state == "agent_unresponsive" and obs.level == "agent"
    assert obs.title == "Guest agent on media is not answering" and "timed out" in obs.reason
    weak = _facts(agent=AgentState.ANSWERS, enabled_commands=["guest-exec", "guest-ping"])
    obs = lockdown.observe(weak, scope)
    assert obs is not None and obs.state == "lockdown_weakened"
    assert obs.key == "node-a/105/lockdown" and "guest-exec" in obs.reason
    for state in (AgentState.NOT_ENABLED, AgentState.NO_PERMISSION, AgentState.NOT_NEEDED):
        assert answers.observe(_facts(agent=state), scope) is None
        assert lockdown.observe(_facts(agent=state), scope) is None
    assert answers.observe(_facts(agent=AgentState.NOT_ANSWERING, running=False), scope) is None


async def test_an_agent_that_stops_answering_is_announced_and_recovers(session: Session) -> None:
    from tests.test_notifications import Recorder, engine_with, feed

    node = Node(name="node-a")
    session.add(node)
    session.commit()
    session.add(Service(slug="media", name="Media", node_id=node.id, guest_kind=GuestKind.VM,
                        guest_id=105))  # fmt: skip
    session.commit()
    engine = engine_with(Recorder())

    async def cycle(state: AgentState) -> list[Any]:
        facts = _facts(agent=state, agent_detail="no answer")
        return (await feed(engine, session, checks.observations(session, [facts])))[0]

    assert await cycle(AgentState.ANSWERS) == []  # first sight
    assert await cycle(AgentState.NOT_ANSWERING) == []  # one read is not enough
    (down,) = await cycle(AgentState.NOT_ANSWERING)
    assert down.event == "agent_unresponsive" and down.severity == "warn"
    from orchestrator.notify.links import link_for

    assert link_for("agent", "node-a/105/lockdown", "lockdown_weakened") == "/sources"  # no source
    assert [a.key for a in checks.open_alerts(session, "node-a/105")] == ["node-a/105"]
    await cycle(AgentState.ANSWERS)
    (back,) = await cycle(AgentState.ANSWERS)
    assert back.event == "recovered"
    assert checks.open_alerts(session, "node-a/105") == []


# -- setup steps -----------------------------------------------------------------------------------


def test_the_setup_steps_are_only_the_missing_ones() -> None:
    refused = setup_guide.build(_facts(agent=AgentState.NO_PERMISSION))
    assert [s.title for s in refused.steps] == [
        "Let the orchestrator read guest agents",
        "Press Read now",
    ]
    assert "VM.GuestAgent.Audit" in refused.steps[0].command and refused.checks[0].ok is False
    off = setup_guide.build(_facts(agent=AgentState.NOT_ENABLED, os_type="l26"))
    assert [s.title for s in off.steps] == [
        "Install it in the guest",
        "Lock it down",
        "Turn it on in Proxmox",
        "Stop and start the VM once",
    ]
    assert off.steps[0].command == "apt install qemu-guest-agent" and "VM 105" in off.steps[2].text
    win = setup_guide.build(_facts(agent=AgentState.NOT_ENABLED, os_type="win11"))
    assert win.os_family == "windows" and "VirtIO" in win.steps[0].text
    weak = setup_guide.build(
        _facts(agent=AgentState.ANSWERS, enabled_commands=["guest-exec"], ping_ms=9.0)
    )
    assert [s.title for s in weak.steps] == ["Lock it down"] and not weak.complete
    done = setup_guide.build(_facts(agent=AgentState.ANSWERS, enabled_commands=["guest-ping"]))
    assert done.complete and done.steps == []


# -- what it fills in ------------------------------------------------------------------------------


async def test_disks_use_the_same_reading() -> None:
    px = DryRunProxmoxAdapter([Guest("node-a", 105, GuestKind.VM, "media", "running"),
                               Guest("node-a", 121, GuestKind.VM, "wiki", "running")])  # fmt: skip
    facts = {
        "node-a/105": _facts(
            agent=AgentState.ANSWERS,
            filesystems=[{"mount": "/", "fstype": "ext4", "used": 90, "total": 100}],
        ),
        "node-a/121": _facts(
            ref=GuestRef("", "node-a/121"),
            agent=AgentState.NOT_ENABLED,
            agent_detail="the QEMU Guest Agent option is off",
        ),
    }
    status = await disks.check_disks(px, get_settings(), facts=facts)
    by = {g.vmid: g for g in status.guests}
    assert by[105].state == "low" and by[105].worst["percent"] == 90.0
    assert by[121].state == "unmeasured" and "option is off" in (by[121].error or "")
    assert px.agent_calls == []  # nothing asked twice


async def test_ip_discovery_takes_the_address_from_the_reading(session: Session) -> None:
    from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus
    from orchestrator.ipdiscovery.discovery import IpDiscoverer

    node = Node(name="node-a")
    session.add(node)
    session.commit()
    svc = Service(slug="media", name="Media", node_id=node.id, guest_kind=GuestKind.VM,
                  guest_id=105)  # fmt: skip
    wiki = Service(slug="wiki", name="Wiki", node_id=node.id, guest_kind=GuestKind.VM,
                   guest_id=121)  # fmt: skip
    session.add_all([svc, wiki])
    session.commit()
    now = datetime.now(UTC).isoformat()
    store.save_many(session, [
        _facts(agent=AgentState.ANSWERS, addresses=["10.0.2.45"], macs=[MAC],
               address_source="guest_agent", read_at=now),
        _facts(ref=GuestRef("", "node-a/121"), agent=AgentState.NOT_ENABLED, macs=[MAC],
               agent_detail="the QEMU Guest Agent option is off for this VM in Proxmox",
               read_at=now),
    ])  # fmt: skip
    session.commit()
    px = DryRunProxmoxAdapter([Guest("node-a", 105, GuestKind.VM, "media", "running"),
                               Guest("node-a", 121, GuestKind.VM, "wiki", "running")])  # fmt: skip
    px.simulated_cluster = ClusterStatus(nodes=[ClusterNode("node-a", True, local=True)])
    disc = IpDiscoverer(settings=get_settings(), proxmox=px)
    found = await disc.discover(session, svc)
    assert (found.status, found.ip, found.source) == ("found", "10.0.2.45", "guest_agent")
    missed = await disc.discover(session, wiki)
    assert missed.status == "not_found" and "option is off" in missed.message
    assert px.guest_network_calls == []  # the reading was used, the agent not asked again
    assert session.get(ServiceState, svc.id).discovered_ip == "10.0.2.45"  # type: ignore[union-attr]

    # an old reading is not trusted: Proxmox is asked as before
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    store.save(session, _facts(agent=AgentState.ANSWERS, addresses=["10.0.2.99"], read_at=old))
    session.commit()
    await disc.discover(session, svc)
    assert px.guest_network_calls == [("node-a", 105, GuestKind.VM)]


def test_the_row_view() -> None:
    f = _facts(agent=AgentState.ANSWERS, addresses=["10.0.2.45"], address_source="guest_agent",
               os_name="Debian GNU/Linux", os_version="12", enabled_commands=["guest-exec"])  # fmt: skip
    v = guest_view.summary(f)
    assert v["address"] == {"ip": "10.0.2.45", "source": "guest_agent", "source_label": "agent"}
    assert v["os"] == "Debian 12" and v["needs_look"] and v["dangerous"] == ["guest-exec"]
    none = guest_view.summary(None, {"ip": "10.0.2.12", "source": "arp", "source_label": "ARP"})
    assert none["address"]["source"] == "arp" and none["state"] == "unknown"


# -- the screens -----------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from orchestrator.web.app import create_app
    from tests.test_proxy_hosts import _login_as_first_admin

    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_the_guests_tab_drawer_and_read_now(client: TestClient) -> None:
    from orchestrator.web.routers import infra

    infra._read_now_at.clear()
    px = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    px.simulated_guests = [Guest("node-a", 105, GuestKind.VM, "media", "running"),
                           Guest("node-a", 114, GuestKind.VM, "games", "running")]  # fmt: skip
    _answering(px, 105)
    px.simulated_configs[114] = {"agent": "1"}
    px.simulated_agent[114] = {"ping": AdapterAuthError("forbidden", status_code=403)}

    rows = {g["vmid"]: g for g in client.get("/api/infra/proxmox/guests").json()}
    assert rows[105]["agent"]["state"] == "unknown"  # not read yet

    r = client.post("/api/infra/proxmox/guests/node-a/105/agent/read")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["state"] == "answers" and d["address"]["ip"] == "10.0.2.45"
    assert [s["user"] for s in d["sessions"]] == ["admin", "backup"]  # shortest first
    assert d["guide"]["complete"] and d["proxmox"]["freeze_on_backup"] is True
    again = client.post("/api/infra/proxmox/guests/node-a/105/agent/read")
    assert again.status_code == 429 and "read again in" in again.json()["detail"]

    refused = client.post("/api/infra/proxmox/guests/node-a/114/agent/read").json()
    assert refused["state"] == "no_permission"
    assert refused["guide"]["steps"][0]["title"] == "Let the orchestrator read guest agents"

    rows = {g["vmid"]: g for g in client.get("/api/infra/proxmox/guests").json()}
    assert rows[105]["agent"]["sessions"][0]["user"] == "admin"
    assert rows[114]["agent"]["label"] == "no permission"
    assert client.get("/api/infra/proxmox/guests/node-a/105/agent").json()["state"] == "answers"
    assert client.post("/api/infra/proxmox/guests/node-a/999/agent/read").status_code == 404

    js = client.get("/static/source_proxmox.js").text
    assert "Agent needs a look" in js and '"/read"' in js and "Set up agent" in js


def test_the_agent_endpoints_need_a_signed_in_admin() -> None:
    from orchestrator.web.app import create_app

    session_module.create_all_for_tests()
    with TestClient(create_app()) as anon:
        assert anon.get("/api/infra/proxmox/guests/node-a/105/agent").status_code in (401, 403)
        r = anon.post("/api/infra/proxmox/guests/node-a/105/agent/read")
        assert r.status_code in (401, 403)


def test_a_service_links_to_its_guest_with_its_open_alerts(session: Session) -> None:
    from orchestrator.registry.tree import TreeIndex
    from orchestrator.web.routers.views import guest_link

    node = Node(name="node-a", source_id="pve")
    session.add(node)
    session.commit()
    root = Service(slug="media", name="Media", node_id=node.id, guest_kind=GuestKind.VM,
                   guest_id=105)  # fmt: skip
    session.add(root)
    session.commit()
    sub = Service(slug="jelly", name="Jelly", parent_id=root.id, node_id=node.id)
    session.add(sub)
    session.add(TargetState(level="agent", key="node-a/105", state="agent_unresponsive",
                            announced="agent_unresponsive"))  # fmt: skip
    session.add(TargetState(level="agent", key="node-a/1050", state="agent_unresponsive",
                            announced="agent_unresponsive"))  # another guest  # fmt: skip
    session.commit()
    tree = TreeIndex.build(session)
    link = guest_link(session, tree, root)
    assert link is not None and link["key"] == "node-a/105" and link["alerts"] == 1
    assert link["href"] == "/sources/pve?tab=guests&q=105&guest=node-a%2F105"
    assert guest_link(session, tree, sub)["key"] == "node-a/105"  # type: ignore[index]


# -- PR 2: filesystems, network, frozen, sign-ins, address --------------------------------------


def test_read_only_images_and_network_filesystems_are_not_disk_space() -> None:
    from orchestrator.adapters.proxmox.disks import network_mounts, parse_fsinfo

    payload = [
        {"name": "dm-0", "mountpoint": "/", "type": "erofs", "total-bytes": 10, "used-bytes": 10},
        {"name": "sda8", "mountpoint": "/mnt/data", "type": "ext4", "total-bytes": 100,
         "used-bytes": 77},
        {"name": "nas:/lib", "mountpoint": "/mnt/library", "type": "nfs4", "total-bytes": 900,
         "used-bytes": 899},
    ]  # fmt: skip
    assert [f.mount for f in parse_fsinfo(payload)] == ["/mnt/data"]
    assert [f.mount for f in parse_fsinfo(payload, include_network=True)] == [
        "/mnt/data",
        "/mnt/library",
    ]
    assert network_mounts(payload) == [{"mount": "/mnt/library", "fstype": "nfs4"}]


async def test_the_reading_lists_network_mounts_apart() -> None:
    px = DryRunProxmoxAdapter()
    _answering(px, 105, **{"get-fsinfo": [
        {"name": "sda1", "mountpoint": "/", "type": "ext4", "total-bytes": 100, "used-bytes": 40},
        {"name": "nas", "mountpoint": "/mnt/library", "type": "nfs", "total-bytes": 9,
         "used-bytes": 9},
    ]})  # fmt: skip
    f = await _read(px, 105)
    assert [x["mount"] for x in f.filesystems or []] == ["/"]
    assert f.network_mounts == [{"mount": "/mnt/library", "fstype": "nfs"}]
    counted = await ProxmoxGuestProvider(px, include_network_fs=True).read(
        _target(105), None, os_due=False
    )
    assert [x["mount"] for x in counted.filesystems or []] == ["/", "/mnt/library"]


def _iface(name: str, mac: str, *addresses: str) -> Any:
    from orchestrator.guests.model import Interface

    return Interface(name=name, mac=mac, addresses=list(addresses))


def test_the_network_check_watches_the_guests_own_card() -> None:
    scope = checks.Scope(node="node-a")
    check = checks.NetworkCheck()
    up = _facts(agent=AgentState.ANSWERS, macs=[MAC],
                interfaces=[_iface("eth0", MAC, "10.0.2.45/24"), _iface("docker0", "02:42:00:00:00:01")])  # fmt: skip
    assert check.observe(up, scope).state == "healthy"  # type: ignore[union-attr]
    lost = _facts(agent=AgentState.ANSWERS, macs=[MAC],
                  interfaces=[_iface("eth0", MAC), _iface("docker0", "02:42:00:00:00:01", "172.17.0.1/16")])  # fmt: skip
    obs = check.observe(lost, scope)
    assert obs is not None and obs.state == "network_down" and obs.severity == "error"
    assert obs.key == "node-a/105/network" and "eth0" in obs.reason
    none = _facts(agent=AgentState.ANSWERS, macs=[], interfaces=[])
    assert check.observe(none, scope).state == "network_down"  # type: ignore[union-attr]
    assert check.observe(_facts(agent=AgentState.NOT_ANSWERING), scope) is None
    assert check.observe(_facts(agent=AgentState.ANSWERS), scope) is None  # interfaces not read


def test_the_frozen_check() -> None:
    scope = checks.Scope(node="node-a")
    check = checks.FrozenCheck()
    obs = check.observe(_facts(agent=AgentState.ANSWERS, freeze="frozen"), scope)
    assert obs is not None and obs.state == "guest_frozen" and obs.severity == "error"
    assert "qm guest cmd 105 fsfreeze-thaw" in obs.reason and obs.key == "node-a/105/frozen"
    thawed = check.observe(_facts(agent=AgentState.ANSWERS, freeze="thawed"), scope)
    assert thawed is not None and thawed.state == "healthy"
    assert check.observe(_facts(agent=AgentState.ANSWERS), scope) is None


def _signed(*who: tuple[str, int]) -> list[Any]:
    from orchestrator.guests.model import SignedIn

    now = datetime.now(UTC)
    return [SignedIn(user=u, since=(now - timedelta(seconds=s)).isoformat()) for u, s in who]


def test_sign_ins_and_sign_outs_are_moments_between_two_readings() -> None:
    scope = checks.Scope(node="node-a", slug="media")
    check = checks.SignInEvents()
    admin, backup = _signed(("admin", 720), ("backup", 3 * 86400 + 4 * 3600))
    now = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    before = _facts(agent=AgentState.ANSWERS, sessions=[backup], read_at=now)
    after = _facts(agent=AgentState.ANSWERS, sessions=[admin], read_at=now)
    assert check.events(None, after, scope) == []  # first reading: nobody announced
    ev = {e.event: e for e in check.events(before, after, scope)}
    assert ev["guest_signin"].title == "admin signed in to media"
    assert ev["guest_signout"].title == "backup signed out of media"
    assert "after 3 d 4 h" in ev["guest_signout"].body
    assert all(e.level == "agent" and e.severity == "info" for e in ev.values())
    unknown = _facts(agent=AgentState.NOT_ANSWERING)
    assert check.events(before, unknown, scope) == []  # not listed: say nothing
    assert check.events(unknown, after, scope) == []  # back from not answering: nothing


def test_an_address_change_is_a_moment() -> None:
    scope = checks.Scope(node="node-a")
    check = checks.AddressEvents()
    a = _facts(agent=AgentState.ANSWERS, addresses=["10.0.2.45"])
    b = _facts(agent=AgentState.ANSWERS, addresses=["10.0.2.46"])
    (ev,) = check.events(a, b, scope)
    assert ev.event == "ip_changed" and "10.0.2.45" in ev.body and "10.0.2.46" in ev.title
    assert check.events(a, a, scope) == [] and check.events(None, b, scope) == []
    assert check.events(a, _facts(agent=AgentState.NOT_ANSWERING), scope) == []


def test_the_new_events_are_known_and_sign_ins_sent_by_default() -> None:
    from orchestrator.notify.engine import DEFAULT_EVENTS, EVENTS

    for e in ("network_down", "guest_frozen", "guest_signin", "guest_signout", "ip_changed"):
        assert e in EVENTS
    assert {"network_down", "guest_frozen", "guest_signin", "ip_changed"} <= set(DEFAULT_EVENTS)
    assert "guest_signout" not in DEFAULT_EVENTS  # in the feed, sent only when a rule asks


async def test_a_cycle_announces_who_signed_in(session: Session) -> None:
    from orchestrator.guests import runner
    from tests.test_notifications import Recorder, engine_with

    px = DryRunProxmoxAdapter()
    _answering(px, 105, **{"get-users": []})
    guests = [Guest("node-a", 105, GuestKind.VM, "media", "running")]
    rec = Recorder()
    engine = engine_with(rec)
    await runner.collect(px, get_settings(), guests, engine)
    assert not [t for _, _, t in rec.sent if "signed in" in t]
    px.simulated_agent[105]["get-users"] = [{"user": "admin", "login-time": time.time() - 60}]
    await runner.collect(px, get_settings(), guests, engine)
    assert [t for _, _, t in rec.sent if "signed in" in t] == ["admin signed in to media"]


async def test_after_a_backup_a_frozen_vm_is_kept_and_checked(session: Session) -> None:
    from orchestrator.guests import runner
    from orchestrator.notify import hooks
    from tests.test_notifications import Recorder, engine_with

    px = DryRunProxmoxAdapter()
    _answering(px, 105, **{"fsfreeze-status": "frozen"})
    store.save(session, _facts(ref=GuestRef("", "node-a/105"), agent=AgentState.ANSWERS,
                               freeze="thawed"))  # fmt: skip
    session.commit()
    engine = engine_with(Recorder())
    hooks.install(engine)
    try:
        assert await runner.after_backup(px, get_settings(), "node-a", 105) == "frozen"
    finally:
        hooks.install(None)
    session.expire_all()
    assert store.load_by_key(session, "node-a/105").freeze == "frozen"  # type: ignore[union-attr]
    row = session.get(TargetState, ("agent", "node-a/105/frozen"))
    assert row is not None  # the frozen check saw it at once
    # a VM whose agent cannot say: nothing recorded, the backup goes on
    assert await runner.after_backup(px, get_settings(), "node-a", 999) is None
