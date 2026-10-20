"""Service IP discovery: Proxmox config / guest-agent parsing, the live
adapter's degrade paths, ARP and DHCP table parsing, the discovery order, the
periodic scheduler and the ping probe. API / CLI / UI live in
``test_service_ip_api.py``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterAuthError, AdapterRequestError
from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest, GuestNetwork
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.adapters.proxmox.net import (
    normalize_mac,
    parse_agent_interfaces,
    parse_lxc_interfaces,
    parse_lxc_net,
    parse_qemu_net_macs,
    qemu_agent_enabled,
)
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, Node, Probe, Service, ServiceState
from orchestrator.domain.enums import GuestKind, HealthState, ProbeKind
from orchestrator.health.engine import HealthEngine
from orchestrator.health.transports.base import CommandResult, TransportUnreachable
from orchestrator.health.transports.dry_run import DryRunTransport
from orchestrator.ipdiscovery.dhcp import DnsmasqLeaseFile, leases_by_mac, parse_dnsmasq_leases
from orchestrator.ipdiscovery.discovery import (
    IpDiscoverer,
    effective_ip,
    parse_transport_setting,
)
from orchestrator.ipdiscovery.neighbors import (
    parse_arp_an,
    parse_ip_neigh,
    parse_sweep_cidrs,
    read_arp_table,
    sweep,
)

MAC_A = "bc:24:11:aa:bb:01"
MAC_B = "bc:24:11:aa:bb:02"

# ---------------------------------------------------------------------------
# config / agent parsing
# ---------------------------------------------------------------------------


def test_qemu_config_macs_for_several_nic_models() -> None:
    config = {
        "net0": "virtio=BC:24:11:AA:BB:01,bridge=vmbr0,firewall=1",
        "net1": "e1000=bc-24-11-aa-bb-02,bridge=vmbr1,tag=20",
        "net2": "rtl8139=BC:24:11:AA:BB:03,bridge=vmbr0",
        "net10": "vmxnet3=BC:24:11:AA:BB:04,bridge=vmbr0",
        "scsi0": "local-lvm:vm-100-disk-0,size=32G",
        "name": "media",
    }
    assert parse_qemu_net_macs(config) == [
        MAC_A,
        MAC_B,
        "bc:24:11:aa:bb:03",
        "bc:24:11:aa:bb:04",
    ]


def test_qemu_agent_enabled_spellings() -> None:
    assert qemu_agent_enabled({"agent": "1"})
    assert qemu_agent_enabled({"agent": "enabled=1,fstrim_cloned_disks=1"})
    assert qemu_agent_enabled({"agent": "1,type=virtio"})
    assert not qemu_agent_enabled({"agent": "0"})
    assert not qemu_agent_enabled({"agent": "type=virtio"})
    assert not qemu_agent_enabled({})


def test_lxc_config_static_ip_is_an_answer_dhcp_is_not() -> None:
    static = {
        "net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:01,ip=10.0.0.21/24,gw=10.0.0.1,"
        "ip6=fd00::21/64,type=veth"
    }
    assert parse_lxc_net(static) == ([MAC_A], ["10.0.0.21", "fd00::21"])
    dhcp = {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:02,ip=dhcp,ip6=auto,type=veth"}
    assert parse_lxc_net(dhcp) == ([MAC_B], [])


AGENT_REPLY = {
    "result": [
        {
            "name": "lo",
            "hardware-address": "00:00:00:00:00:00",
            "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}],
        },
        {
            "name": "docker0",
            "hardware-address": "02:42:ac:11:00:01",
            "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "172.17.0.1"}],
        },
        {
            "name": "eth0",
            "hardware-address": MAC_A,
            "ip-addresses": [
                {"ip-address-type": "ipv6", "ip-address": "fe80::be24:11ff:feaa:bb01"},
                {"ip-address-type": "ipv6", "ip-address": "fd00::21"},
                {"ip-address-type": "ipv4", "ip-address": "10.0.0.21"},
            ],
        },
    ]
}


def test_agent_interfaces_skip_loopback_link_local_and_prefer_the_config_mac() -> None:
    # The config NIC's addresses only (docker0 dropped), IPv4 first, no fe80::.
    assert parse_agent_interfaces(AGENT_REPLY, [MAC_A]) == ["10.0.0.21", "fd00::21"]
    # Without a MAC match every non-loopback address is a candidate.
    assert parse_agent_interfaces(AGENT_REPLY, []) == ["172.17.0.1", "10.0.0.21", "fd00::21"]


def test_lxc_interfaces_parse() -> None:
    rows = [
        {"name": "lo", "hwaddr": "00:00:00:00:00:00", "inet": "127.0.0.1/8"},
        {"name": "eth0", "hwaddr": MAC_B, "inet": "10.0.0.31/24", "inet6": "fe80::1/64"},
    ]
    assert parse_lxc_interfaces(rows, [MAC_B]) == ["10.0.0.31"]


def test_normalize_mac_rejects_garbage_and_null() -> None:
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("vmbr0") is None
    assert normalize_mac("00:00:00:00:00:00") is None


# ---------------------------------------------------------------------------
# live adapter: graceful degrade
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.paths: list[str] = []

    async def request(self, method: str, path: str, **_: Any) -> Any:
        self.paths.append(path)
        answer = self.answers.get(path)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _live(answers: dict[str, Any]) -> tuple[LiveProxmoxAdapter, _FakeClient]:
    adapter = LiveProxmoxAdapter(get_settings())
    fake = _FakeClient(answers)
    adapter._client = fake  # type: ignore[assignment]
    return adapter, fake


async def test_live_qemu_guest_agent_answer() -> None:
    adapter, _ = _live(
        {
            "/nodes/pve1/qemu/100/config": {"net0": f"virtio={MAC_A},bridge=vmbr0", "agent": "1"},
            "/nodes/pve1/qemu/100/agent/network-get-interfaces": AGENT_REPLY,
        }
    )
    net = await adapter.guest_network("pve1", 100, GuestKind.VM)
    assert net == GuestNetwork(macs=[MAC_A], ips=["10.0.0.21", "fd00::21"], source="guest_agent")


async def test_live_agent_not_running_is_a_normal_miss() -> None:
    adapter, _ = _live(
        {
            "/nodes/pve1/qemu/100/config": {"net0": f"virtio={MAC_A},bridge=vmbr0", "agent": "1"},
            "/nodes/pve1/qemu/100/agent/network-get-interfaces": AdapterRequestError(
                "pve returned 500", status_code=500, body="QEMU guest agent is not running"
            ),
        }
    )
    net = await adapter.guest_network("pve1", 100, GuestKind.VM)
    assert net == GuestNetwork(macs=[MAC_A])  # MACs kept for ARP / DHCP, no error


async def test_live_agent_not_configured_is_not_asked() -> None:
    adapter, fake = _live({"/nodes/pve1/qemu/100/config": {"net0": f"virtio={MAC_A}"}})
    net = await adapter.guest_network("pve1", 100, GuestKind.VM)
    assert net.macs == [MAC_A] and not net.ips
    assert fake.paths == ["/nodes/pve1/qemu/100/config"]


async def test_live_403_on_config_names_vm_audit() -> None:
    adapter, _ = _live(
        {"/nodes/pve1/qemu/100/config": AdapterAuthError("nope", status_code=403, body="x")}
    )
    net = await adapter.guest_network("pve1", 100, GuestKind.VM)
    assert not net.macs and not net.ips
    assert net.error and "VM.Audit" in net.error


async def test_live_403_on_agent_names_vm_monitor_and_keeps_macs() -> None:
    adapter, _ = _live(
        {
            "/nodes/pve1/qemu/100/config": {"net0": f"virtio={MAC_A}", "agent": "1"},
            "/nodes/pve1/qemu/100/agent/network-get-interfaces": AdapterAuthError(
                "nope", status_code=403
            ),
        }
    )
    net = await adapter.guest_network("pve1", 100, GuestKind.VM)
    assert net.macs == [MAC_A] and not net.ips
    assert net.error and "VM.Monitor" in net.error


async def test_live_lxc_static_ip_skips_interfaces() -> None:
    adapter, fake = _live(
        {"/nodes/pve1/lxc/200/config": {"net0": f"name=eth0,hwaddr={MAC_B},ip=10.0.0.31/24"}}
    )
    net = await adapter.guest_network("pve1", 200, GuestKind.CT)
    assert net == GuestNetwork(macs=[MAC_B], ips=["10.0.0.31"], source="lxc")
    assert fake.paths == ["/nodes/pve1/lxc/200/config"]


async def test_live_lxc_dhcp_uses_interfaces() -> None:
    adapter, _ = _live(
        {
            "/nodes/pve1/lxc/200/config": {"net0": f"name=eth0,hwaddr={MAC_B},ip=dhcp"},
            "/nodes/pve1/lxc/200/interfaces": [
                {"name": "eth0", "hwaddr": MAC_B, "inet": "10.0.0.32/24"}
            ],
        }
    )
    net = await adapter.guest_network("pve1", 200, GuestKind.CT)
    assert net == GuestNetwork(macs=[MAC_B], ips=["10.0.0.32"], source="lxc")


# ---------------------------------------------------------------------------
# ARP / DHCP tables
# ---------------------------------------------------------------------------

IP_NEIGH = f"""\
10.0.0.1 dev eth0 lladdr 52:54:00:00:00:01 REACHABLE
10.0.0.21 dev eth0 lladdr {MAC_A.upper()} STALE
10.0.0.99 dev eth0  FAILED
10.0.0.98 dev eth0 INCOMPLETE
10.0.0.97 dev eth0 lladdr {MAC_B} FAILED
fe80::1 dev eth0 lladdr {MAC_A} router STALE
"""

ARP_AN = f"""\
? (10.0.0.1) at 52:54:00:00:00:01 [ether] on eth0
? (10.0.0.22) at {MAC_B} [ether] on eth0
? (10.0.0.50) at <incomplete> on eth0
"""


def test_parse_ip_neigh_ignores_failed_incomplete_and_link_local() -> None:
    table = parse_ip_neigh(IP_NEIGH)
    assert table == {"52:54:00:00:00:01": ["10.0.0.1"], MAC_A: ["10.0.0.21"]}


def test_parse_arp_an() -> None:
    table = parse_arp_an(ARP_AN)
    assert table == {"52:54:00:00:00:01": ["10.0.0.1"], MAC_B: ["10.0.0.22"]}


class _MissingBinaries(DryRunTransport):
    """``ip`` is not installed: the transport cannot run it at all."""

    def __init__(self, missing: set[str], **kw: Any) -> None:
        super().__init__(**kw)
        self.missing = missing

    async def run(self, command: Any, *, timeout_s: int, env: Any = None) -> CommandResult:
        if command[0] in self.missing:
            self.calls.append(list(command))
            raise TransportUnreachable(f"local: executable not found: {command[0]!r}")
        return await super().run(command, timeout_s=timeout_s, env=env)


async def test_read_arp_table_falls_back_to_arp_an() -> None:
    t = _MissingBinaries({"ip"})
    t.script("arp -an", CommandResult(0, stdout=ARP_AN))
    table = await read_arp_table(t)
    assert table[MAC_B] == ["10.0.0.22"]
    assert [c[0] for c in t.calls] == ["ip", "arp"]


def test_parse_dnsmasq_leases_drops_expired_and_v6_lines() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    future = int((now + timedelta(hours=2)).timestamp())
    past = int((now - timedelta(hours=2)).timestamp())
    text = (
        f"{future} {MAC_A} 10.0.0.21 media 01:{MAC_A}\n"
        f"{past} {MAC_B} 10.0.0.22 old *\n"
        f"0 52:54:00:00:00:09 10.0.0.9 * *\n"
        "duid 00:01:00:01:2c:aa:bb:cc:52:54:00:00:00:01\n"
        f"{future} 12345678 fd00::50 host6 00:02:00\n"
    )
    leases = parse_dnsmasq_leases(text, now=now)
    assert [(lease.mac, lease.ip, lease.hostname) for lease in leases] == [
        (MAC_A, "10.0.0.21", "media"),
        ("52:54:00:00:00:09", "10.0.0.9", ""),
    ]
    assert leases_by_mac(leases)[MAC_A] == ["10.0.0.21"]


async def test_dnsmasq_lease_file_reads_over_transport() -> None:
    t = DryRunTransport()
    t.script("cat /var/lib/misc/dnsmasq.leases", CommandResult(0, stdout=f"0 {MAC_A} 10.0.0.5 h *"))
    leases = await DnsmasqLeaseFile(t, "/var/lib/misc/dnsmasq.leases").leases()
    assert leases[0].ip == "10.0.0.5"
    assert t.calls == [["cat", "/var/lib/misc/dnsmasq.leases"]]


def test_sweep_cidrs_are_capped() -> None:
    assert [str(n) for n in parse_sweep_cidrs("10.0.0.7/24, 10.1.0.0/22")] == [
        "10.0.0.0/24",
        "10.1.0.0/22",
    ]
    with pytest.raises(ValueError, match="capped"):
        parse_sweep_cidrs("10.0.0.0/16")


async def test_sweep_uses_fping_when_present() -> None:
    t = DryRunTransport()
    t.script("fping", CommandResult(1))  # 1 = some hosts unreachable: still ran
    assert await sweep(t, parse_sweep_cidrs("10.0.0.0/24")) == []
    assert t.calls[0][:1] == ["fping"] and t.calls[0][-1] == "10.0.0.0/24"
    assert len(t.calls) == 1


async def test_sweep_falls_back_to_a_bounded_ping_loop() -> None:
    t = _MissingBinaries({"fping"})
    assert await sweep(t, parse_sweep_cidrs("10.0.0.0/25")) == []
    loop = t.calls[-1]
    assert loop[:2] == ["sh", "-c"]
    script = loop[2]
    assert "ping -c1 -W1" in script and "10.0.0.1 " in script and "10.0.0.126" in script
    assert script.count("wait") == 2  # 126 hosts -> two batches of <= 64


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def _world(
    *services: tuple[str, int, GuestKind, str],
) -> DryRunProxmoxAdapter:
    """(slug, vmid, kind, status) per service, all on node pve1."""
    session_module.create_all_for_tests()
    px = DryRunProxmoxAdapter(
        simulated_guests=[
            Guest("pve1", vmid, kind, slug, status) for slug, vmid, kind, status in services
        ]
    )
    px.simulated_cluster = ClusterStatus(nodes=[ClusterNode("pve1", True, local=True)])
    with Session(session_module.get_engine()) as s:
        node = Node(name="pve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        for slug, vmid, kind, _status in services:
            s.add(Service(slug=slug, name=slug, node_id=node.id, guest_kind=kind, guest_id=vmid))
        s.commit()
    return px


def _svc(session: Session, slug: str) -> Service:
    return session.exec(select(Service).where(Service.slug == slug)).one()


def _arp_transport(table_text: str = IP_NEIGH, leases: str = "") -> DryRunTransport:
    t = DryRunTransport()
    t.script("ip neigh", CommandResult(0, stdout=table_text))
    t.script("cat /leases", CommandResult(0, stdout=leases))
    return t


def _discoverer(
    px: DryRunProxmoxAdapter, transport: DryRunTransport | None, leases: bool = False
) -> IpDiscoverer:
    settings = get_settings()
    if leases:
        settings.ip_discovery_dhcp_leases_path = "/leases"
    return IpDiscoverer(settings=settings, proxmox=px, transport=transport)


async def test_guest_agent_answer_wins_and_is_recorded() -> None:
    px = _world(("media", 100, GuestKind.VM, "running"))
    px.simulated_networks[100] = GuestNetwork(macs=[MAC_A], ips=["10.0.0.40"], source="guest_agent")
    t = _arp_transport()
    with Session(session_module.get_engine()) as s:
        r = await _discoverer(px, t).discover(s, _svc(s, "media"))
        assert (r.status, r.ip, r.source) == ("found", "10.0.0.40", "guest_agent")
        state = s.get(ServiceState, _svc(s, "media").id)
        assert state is not None
        assert (state.discovered_ip, state.ip_source, state.mac_addresses) == (
            "10.0.0.40",
            "guest_agent",
            [MAC_A],
        )
        assert state.ip_discovered_at is not None
    assert t.calls == []  # Proxmox answered: ARP never read


async def test_arp_answers_when_the_agent_cannot_then_dhcp() -> None:
    px = _world(("media", 100, GuestKind.VM, "running"), ("photos", 101, GuestKind.VM, "running"))
    px.simulated_networks[100] = GuestNetwork(macs=[MAC_A])  # in the ARP table
    px.simulated_networks[101] = GuestNetwork(macs=["bc:24:11:aa:bb:77"])  # only in DHCP
    t = _arp_transport(leases="0 bc:24:11:aa:bb:77 10.0.0.77 photos *\n")
    d = _discoverer(px, t, leases=True)
    cycle = d.new_cycle()
    with Session(session_module.get_engine()) as s:
        r1 = await d.discover(s, _svc(s, "media"), cycle=cycle)
        r2 = await d.discover(s, _svc(s, "photos"), cycle=cycle)
    assert (r1.ip, r1.source) == ("10.0.0.21", "arp")
    assert (r2.ip, r2.source) == ("10.0.0.77", "dhcp")
    # Read ONCE per cycle, shared by both services.
    assert cycle.reads == {"arp": 1, "dhcp": 1}
    assert sum(1 for c in t.calls if c[:2] == ["ip", "neigh"]) == 1
    assert sum(1 for c in t.calls if c[0] == "cat") == 1


async def test_arp_beats_dhcp_for_the_same_mac() -> None:
    px = _world(("media", 100, GuestKind.VM, "running"))
    px.simulated_networks[100] = GuestNetwork(macs=[MAC_A])
    t = _arp_transport(leases=f"0 {MAC_A} 10.0.0.250 stale *\n")
    with Session(session_module.get_engine()) as s:
        r = await _discoverer(px, t, leases=True).discover(s, _svc(s, "media"))
    assert (r.ip, r.source) == ("10.0.0.21", "arp")
    assert not any(c[0] == "cat" for c in t.calls)  # DHCP not needed


async def test_stopped_guest_is_not_asked() -> None:
    px = _world(("media", 100, GuestKind.VM, "stopped"))
    px.simulated_networks[100] = GuestNetwork(macs=[MAC_A], ips=["10.0.0.40"], source="guest_agent")
    with Session(session_module.get_engine()) as s:
        r = await _discoverer(px, _arp_transport()).discover(s, _svc(s, "media"))
        assert r.status == "skipped" and "stopped" in r.message
        assert s.get(ServiceState, _svc(s, "media").id) is None
    assert px.guest_network_calls == []


async def test_manual_ip_wins_and_is_never_overwritten() -> None:
    px = _world(("media", 100, GuestKind.VM, "running"))
    px.simulated_networks[100] = GuestNetwork(macs=[MAC_A], ips=["10.0.0.40"], source="guest_agent")
    with Session(session_module.get_engine()) as s:
        svc = _svc(s, "media")
        svc.ip_address = "192.168.5.5"
        s.add(svc)
        s.commit()
        r = await _discoverer(px, None).discover(s, svc)
        s.refresh(svc)
        assert svc.ip_address == "192.168.5.5"
        assert (r.effective_ip, r.effective_source) == ("192.168.5.5", "manual")
        assert r.changed is False  # discovery moved, the effective IP did not
        state = s.get(ServiceState, svc.id)
        assert state is not None and state.discovered_ip == "10.0.0.40"
        assert effective_ip(svc, state) == ("192.168.5.5", "manual")
        assert not s.exec(select(AuditEntry)).all()


async def test_effective_ip_change_is_audited_once() -> None:
    px = _world(("media", 100, GuestKind.VM, "running"))
    px.simulated_networks[100] = GuestNetwork(macs=[MAC_A], ips=["10.0.0.40"], source="guest_agent")
    d = _discoverer(px, None)
    with Session(session_module.get_engine()) as s:
        assert (await d.discover(s, _svc(s, "media"))).changed is True
        assert (await d.discover(s, _svc(s, "media"))).changed is False
        px.simulated_networks[100] = GuestNetwork(
            macs=[MAC_A], ips=["10.0.0.41"], source="guest_agent"
        )
        await d.discover(s, _svc(s, "media"))
        entries = s.exec(select(AuditEntry).where(AuditEntry.action == "service.ip_changed")).all()
    assert [(e.details["old"], e.details["new"]) for e in entries] == [
        ("", "10.0.0.40"),
        ("10.0.0.40", "10.0.0.41"),
    ]
    assert {e.actor for e in entries} == {"system"}


async def test_nothing_found_keeps_the_last_ip_and_says_why() -> None:
    px = _world(("media", 100, GuestKind.VM, "running"))
    px.simulated_networks[100] = GuestNetwork(macs=[MAC_A], ips=["10.0.0.40"], source="guest_agent")
    d = _discoverer(px, None)
    with Session(session_module.get_engine()) as s:
        await d.discover(s, _svc(s, "media"))
        px.simulated_networks[100] = GuestNetwork(macs=[MAC_A])
        r = await d.discover(s, _svc(s, "media"))
        state = s.get(ServiceState, _svc(s, "media").id)
    assert r.status == "not_found"
    assert "IP_DISCOVERY_TRANSPORT not set" in r.message
    assert state is not None and state.discovered_ip == "10.0.0.40"
    assert state.ip_discovery_error == r.message


async def test_service_without_guest_is_skipped() -> None:
    session_module.create_all_for_tests()
    with Session(session_module.get_engine()) as s:
        node = Node(name="pve1")
        s.add(node)
        s.commit()
        svc = Service(slug="nas", name="NAS", node_id=node.id)
        s.add(svc)
        s.commit()
        r = await _discoverer(DryRunProxmoxAdapter(), None).discover(s, svc)
    assert r.status == "skipped" and "manually" in r.message


def test_transport_setting_shapes() -> None:
    assert parse_transport_setting("") is None
    assert parse_transport_setting("local").type == "local"  # type: ignore[union-attr]
    spec = parse_transport_setting('{"type": "host_agent", "host": "gateway"}')
    assert spec is not None and (spec.type, spec.host) == ("host_agent", "gateway")
    with pytest.raises(ValueError, match="JSON"):
        parse_transport_setting("{nope")


async def test_scheduler_cycle_discovers_running_enabled_guests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.adapters.factory import build_adapters
    from orchestrator.scheduler.ip_discovery import IpDiscoveryScheduler

    px = _world(
        ("media", 100, GuestKind.VM, "running"),
        ("photos", 101, GuestKind.CT, "running"),
        ("off", 102, GuestKind.VM, "stopped"),
    )
    px.simulated_networks[100] = GuestNetwork(macs=[MAC_A])
    px.simulated_networks[101] = GuestNetwork(macs=[MAC_B], ips=["10.0.0.31"], source="lxc")
    with Session(session_module.get_engine()) as s:
        node_id = _svc(s, "media").node_id
        s.add(Service(slug="bare", name="Bare", node_id=node_id))  # no guest
        s.commit()
    adapters = build_adapters(get_settings())
    adapters.proxmox = px
    t = _arp_transport()
    sched = IpDiscoveryScheduler(
        settings=get_settings(),
        adapters=adapters,
        discoverer=IpDiscoverer(settings=get_settings(), proxmox=px, transport=t),
    )
    results = {r.slug: r for r in await sched.poll_once()}
    assert set(results) == {"media", "photos", "off"}
    assert (results["media"].ip, results["media"].source) == ("10.0.0.21", "arp")
    assert (results["photos"].ip, results["photos"].source) == ("10.0.0.31", "lxc")
    assert results["off"].status == "skipped"
    assert sum(1 for c in t.calls if c[:2] == ["ip", "neigh"]) == 1


def test_scheduler_start_is_a_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.adapters.factory import build_adapters
    from orchestrator.scheduler.ip_discovery import IpDiscoveryScheduler

    monkeypatch.setenv("IP_DISCOVERY_ENABLED", "false")
    from orchestrator.config import reset_settings_cache

    reset_settings_cache()
    sched = IpDiscoveryScheduler(settings=get_settings(), adapters=build_adapters(get_settings()))
    sched.start()
    assert sched.running is False


# ---------------------------------------------------------------------------
# ping probe
# ---------------------------------------------------------------------------

PING_OK = """\
PING 10.0.0.21 (10.0.0.21) 56(84) bytes of data.
64 bytes from 10.0.0.21: icmp_seq=1 ttl=64 time=0.412 ms

--- 10.0.0.21 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
rtt min/avg/max/mdev = 0.412/2.613/0.412/0.000 ms
"""


class _Factory:
    def __init__(self, transport: DryRunTransport) -> None:
        self.transport = transport
        self.specs: list[Any] = []

    def build(self, spec: Any) -> DryRunTransport:
        self.specs.append(spec)
        return self.transport


def _ping_row(session: Session, service: Service, config: dict[str, Any]) -> Probe:
    row = Probe(
        service_id=service.id,  # type: ignore[arg-type]
        name="ping",
        kind=ProbeKind.PING,
        required=False,
        timeout_s=5,
        config=config,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


async def test_ping_blank_host_targets_the_service_ip(session: Session, service: Service) -> None:
    service.ip_address = "10.0.0.21"
    session.add(service)
    session.commit()
    t = DryRunTransport()
    t.script("ping", CommandResult(0, stdout=PING_OK))
    factory = _Factory(t)
    row = _ping_row(
        session,
        service,
        {"host": "", "count": 2, "transport": {"type": "host_agent", "host": "gw"}},
    )
    result = await HealthEngine(transport_factory=factory).run_probe(row)  # type: ignore[arg-type]
    assert result.state is HealthState.HEALTHY
    assert t.calls == [["ping", "-c", "2", "-W", "5", "10.0.0.21"]]
    assert result.latency_ms == 3  # avg rtt 2.613 ms, rounded
    assert result.details["host_from"] == "service_ip"
    assert factory.specs[0].type == "host_agent" and factory.specs[0].host == "gw"


async def test_ping_uses_the_discovered_ip_when_no_manual_one(
    session: Session, service: Service
) -> None:
    session.add(ServiceState(service_id=service.id, discovered_ip="10.0.0.40", ip_source="arp"))  # type: ignore[arg-type]
    session.commit()
    t = DryRunTransport()
    t.script("ping", CommandResult(0, stdout=PING_OK))
    row = _ping_row(session, service, {})
    result = await HealthEngine(transport_factory=_Factory(t)).run_probe(row)  # type: ignore[arg-type]
    assert result.state is HealthState.HEALTHY
    assert t.calls[0][-1] == "10.0.0.40"


async def test_ping_without_an_ip_is_unknown_with_a_clear_message(
    session: Session, service: Service
) -> None:
    t = DryRunTransport()
    row = _ping_row(session, service, {"host": ""})
    result = await HealthEngine(transport_factory=_Factory(t)).run_probe(row)  # type: ignore[arg-type]
    assert result.state is HealthState.UNKNOWN
    assert "no IP yet" in (result.message or "")
    assert t.calls == []


async def test_ping_no_reply_is_failed_and_explicit_host_is_used(
    session: Session, service: Service
) -> None:
    t = DryRunTransport()
    t.script("ping", CommandResult(1, stdout="1 packets transmitted, 0 received"))
    row = _ping_row(session, service, {"host": "nas.lan", "count": 1})
    result = await HealthEngine(transport_factory=_Factory(t)).run_probe(row)  # type: ignore[arg-type]
    assert result.state is HealthState.FAILED
    assert t.calls[0][-1] == "nas.lan"


async def test_ping_cannot_run_is_unknown(session: Session, service: Service) -> None:
    row = _ping_row(session, service, {"host": "10.0.0.1"})
    t = DryRunTransport(unreachable=True)
    result = await HealthEngine(transport_factory=_Factory(t)).run_probe(row)  # type: ignore[arg-type]
    assert result.state is HealthState.UNKNOWN


async def test_ping_refuses_option_injection(session: Session, service: Service) -> None:
    t = DryRunTransport()
    row = _ping_row(session, service, {"host": "-f"})
    result = await HealthEngine(transport_factory=_Factory(t)).run_probe(row)  # type: ignore[arg-type]
    assert result.state is HealthState.UNKNOWN and t.calls == []


def test_parse_rtt_variants() -> None:
    from orchestrator.health.probes.ping import parse_rtt_ms

    assert parse_rtt_ms(PING_OK) == pytest.approx(2.613)
    assert parse_rtt_ms("round-trip min/avg/max = 1.0/1.5/2.0 ms") == pytest.approx(1.5)
    assert parse_rtt_ms("64 bytes from x: seq=0 ttl=64 time=4.0 ms") == pytest.approx(4.0)
    assert parse_rtt_ms("nothing") is None
