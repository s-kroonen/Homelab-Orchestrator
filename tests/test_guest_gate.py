"""The guest gate: node online + guest running, checked before anything else.

If either is false the probes are pointless, so the scan short-circuits to
DOWN — which is not FAILED (a stopped VM is not evidence of corrupt data) and
not UNKNOWN (we know exactly why). The backup gate always requires it.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import webauthn
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.errors import AdapterUnreachable
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import (
    BackupRecord,
    HealthScanSample,
    Node,
    PipelineRun,
    ProxyHost,
    Service,
    ServiceState,
)
from orchestrator.db.models import Probe as ProbeRow
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import (
    GuestKind,
    HealthState,
    PipelineStatus,
    ProbeKind,
    WakeTrigger,
)
from orchestrator.health.engine import HealthEngine, TransportFactory
from orchestrator.health.transports.dry_run import DryRunTransport
from orchestrator.pipelines.backup import BackupError, BackupPipeline
from orchestrator.pipelines.wake import WakePipeline
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.scheduler.health_scanner import HealthScanner
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app
from orchestrator.web.routers.traffic import build_series

EXAMPLE = Path(__file__).parent.parent / "config" / "services.example.yaml"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FixedTransportFactory(TransportFactory):
    def __init__(self, transport: DryRunTransport) -> None:
        self._t = transport

    def build(self, spec: Any) -> DryRunTransport:
        return self._t


class CountingProxmox(DryRunProxmoxAdapter):
    """Dry-run Proxmox with call counters and an optional outage."""

    def __init__(self, *, nodes: dict[str, bool], guests: list[Guest], down: bool = False):
        super().__init__(simulated_guests=guests)
        self.simulated_cluster = ClusterStatus(
            nodes=[ClusterNode(name=n, online=on) for n, on in nodes.items()]
        )
        self.down = down
        self.cluster_calls = 0
        self.guest_calls = 0

    async def cluster_status(self) -> ClusterStatus:
        self.cluster_calls += 1
        if self.down:
            raise AdapterUnreachable("proxmox unreachable")
        return await super().cluster_status()

    async def list_guests(self, node: str | None = None) -> list[Guest]:
        self.guest_calls += 1
        if self.down:
            raise AdapterUnreachable("proxmox unreachable")
        return await super().list_guests(node)


def _guest(vmid: int, status: str, node: str = "n1") -> Guest:
    return Guest(node=node, vmid=vmid, kind=GuestKind.VM, name=f"g{vmid}", status=status)


def _engine(proxmox: Any, transport: DryRunTransport | None = None) -> HealthEngine:
    return HealthEngine(
        transport_factory=_FixedTransportFactory(transport or DryRunTransport()),
        proxmox=proxmox,
    )


def _node(session: Session, name: str = "n1") -> Node:
    node = Node(name=name, always_on=True)
    session.add(node)
    session.commit()
    session.refresh(node)
    return node


def _service(
    session: Session,
    node: Node,
    slug: str,
    *,
    guest_id: int | None = 101,
    guest_kind: GuestKind = GuestKind.VM,
    depends_on: list[str] | None = None,
    probes: int = 1,
) -> Service:
    svc = Service(
        slug=slug,
        name=slug,
        node_id=node.id,
        guest_kind=guest_kind,
        guest_id=guest_id,
        depends_on=depends_on or [],
    )
    session.add(svc)
    session.commit()
    session.refresh(svc)
    for i in range(probes):
        session.add(
            ProbeRow(
                service_id=svc.id,
                name=f"{slug}-p{i}",
                kind=ProbeKind.SYSTEMD,
                required=True,
                config={"unit": f"{slug}.service"},
            )
        )
    session.commit()
    return svc


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


async def test_running_guest_on_online_node_runs_probes(session: Session) -> None:
    svc = _service(session, _node(session), "web")
    transport = DryRunTransport()
    proxmox = CountingProxmox(nodes={"n1": True}, guests=[_guest(101, "running")])

    verdict = await _engine(proxmox, transport).scan(session, svc)

    assert verdict.state is HealthState.HEALTHY
    assert transport.calls, "probes must run when the guest is up"
    first = verdict.probe_results[0]
    assert (first.probe_name, str(first.kind), first.state) == (
        "guest",
        "guest",
        HealthState.HEALTHY,
    )
    assert "running" in (first.message or "")
    # The guest result never counts as a required probe.
    assert verdict.reason == "all 1 required probe(s) passed"


async def test_stopped_guest_is_down_and_no_probe_runs(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(session, _node(session), "web", probes=2)
    proxmox = CountingProxmox(nodes={"n1": True}, guests=[_guest(101, "stopped")])
    engine = _engine(proxmox)
    called: list[str] = []

    async def _spy(row: ProbeRow) -> Any:
        called.append(row.name)
        raise AssertionError("no probe may run for a stopped guest")

    monkeypatch.setattr(engine, "run_probe", _spy)
    verdict = await engine.scan(session, svc)

    assert called == []
    assert verdict.state is HealthState.DOWN
    assert "guest vm 101 is stopped" in verdict.reason
    assert [r.probe_name for r in verdict.probe_results] == ["guest", "web-p0", "web-p1"]
    assert verdict.probe_results[0].state is HealthState.DOWN
    for skipped in verdict.probe_results[1:]:
        assert skipped.details["skipped"] is True
        assert skipped.message == "skipped — guest stopped"

    # Persisted as DOWN and readable back through the string column.
    session.expire_all()
    state = session.get(ServiceState, svc.id)
    assert state is not None
    assert state.last_verdict is HealthState.DOWN
    assert state.last_probe_results[0]["kind"] == "guest"


async def test_offline_node_is_down_with_node_reason(session: Session) -> None:
    svc = _service(session, _node(session, "storage"), "nas")
    transport = DryRunTransport()
    proxmox = CountingProxmox(
        nodes={"storage": False}, guests=[_guest(101, "unknown", node="storage")]
    )

    verdict = await _engine(proxmox, transport).scan(session, svc)

    assert verdict.state is HealthState.DOWN
    assert "node 'storage' is offline" in verdict.reason
    assert transport.calls == []
    assert verdict.probe_results[1].message == "skipped — node offline"
    # A node that is off needs no guest lookup at all.
    assert proxmox.guest_calls == 0


async def test_unreachable_proxmox_runs_probes_and_says_unknown(session: Session) -> None:
    svc = _service(session, _node(session), "web")
    transport = DryRunTransport()
    proxmox = CountingProxmox(nodes={"n1": True}, guests=[], down=True)

    verdict = await _engine(proxmox, transport).scan(session, svc)

    assert transport.calls, "an unreadable Proxmox must not blank out the probes"
    assert verdict.state is HealthState.HEALTHY
    assert "guest state unknown" in verdict.reason
    assert verdict.probe_results[0].state is HealthState.UNKNOWN


async def test_service_without_guest_is_unaffected(session: Session) -> None:
    svc = _service(session, _node(session), "bare", guest_kind=GuestKind.NONE, guest_id=None)
    proxmox = CountingProxmox(nodes={"n1": True}, guests=[])

    verdict = await _engine(proxmox).scan(session, svc)

    assert verdict.state is HealthState.HEALTHY
    assert [r.probe_name for r in verdict.probe_results] == ["bare-p0"]
    assert proxmox.cluster_calls == proxmox.guest_calls == 0


async def test_engine_without_proxmox_has_no_guest_gate(session: Session) -> None:
    svc = _service(session, _node(session), "web")
    engine = HealthEngine(transport_factory=_FixedTransportFactory(DryRunTransport()))
    verdict = await engine.scan(session, svc)
    assert verdict.state is HealthState.HEALTHY
    assert [r.probe_name for r in verdict.probe_results] == ["web-p0"]


async def test_dependency_down_makes_dependant_unknown(session: Session) -> None:
    node = _node(session)
    _service(session, node, "nas", guest_id=9001)
    app = _service(session, node, "app", guest_id=101, depends_on=["nas"])
    transport = DryRunTransport()
    proxmox = CountingProxmox(
        nodes={"n1": True}, guests=[_guest(9001, "stopped"), _guest(101, "running")]
    )

    verdict = await _engine(proxmox, transport).scan(session, app)

    assert verdict.state is HealthState.UNKNOWN
    assert verdict.reason.startswith(
        "blocked: dependency 'nas' is DOWN (guest vm 9001 is stopped)."
    )
    assert "NOT evidence about 'app'" in verdict.reason
    assert transport.calls == []
    # One Proxmox read served both the dependant and its dependency.
    assert (proxmox.cluster_calls, proxmox.guest_calls) == (1, 1)


# ---------------------------------------------------------------------------
# Scanner: one Proxmox read per cycle
# ---------------------------------------------------------------------------


async def test_scanner_reads_guest_state_once_per_cycle(session: Session) -> None:
    node = _node(session)
    for i in range(5):
        _service(session, node, f"s{i}", guest_id=100 + i)
    proxmox = CountingProxmox(
        nodes={"n1": True},
        guests=[_guest(100 + i, "running" if i % 2 else "stopped") for i in range(5)],
    )
    scanner = HealthScanner(
        settings=config_module.get_settings(), adapters=SimpleNamespace(proxmox=proxmox)
    )
    scanner._engine = _engine(proxmox)

    assert await scanner.poll_once() == 5
    assert (proxmox.cluster_calls, proxmox.guest_calls) == (1, 1)

    with Session(get_engine()) as s:
        states = sorted(r.state.value for r in s.exec(select(HealthScanSample)).all())
    assert states == ["down", "down", "down", "healthy", "healthy"]

    # Next cycle reads again — never a stale cycle's state.
    await scanner.poll_once()
    assert (proxmox.cluster_calls, proxmox.guest_calls) == (2, 2)


def test_scanner_engine_gets_the_proxmox_adapter() -> None:
    proxmox = DryRunProxmoxAdapter()
    scanner = HealthScanner(
        settings=config_module.get_settings(), adapters=SimpleNamespace(proxmox=proxmox)
    )
    assert scanner._engine._proxmox is proxmox


# ---------------------------------------------------------------------------
# Backup gate: always requires a running guest
# ---------------------------------------------------------------------------


@pytest.fixture
def example_session(session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    config_module.reset_settings_cache()
    yaml_path = get_settings().services_yaml_path
    shutil.copy(EXAMPLE, yaml_path)
    reconcile_yaml_into_db(session, yaml_path)
    session.commit()
    return session


async def test_backup_gate_refuses_down_and_does_not_dump(example_session: Session) -> None:
    proxmox = CountingProxmox(
        nodes={"compute-a": True}, guests=[_guest(9001, "stopped", node="compute-a")]
    )
    # No ``health=``: the pipeline's default engine must carry the guest gate.
    pipeline = BackupPipeline(proxmox=proxmox, pbs=DryRunPbsAdapter(), settings=get_settings())

    with pytest.raises(BackupError) as exc:
        await pipeline.run_for_service(example_session, "example-media")

    msg = str(exc.value)
    assert "guest is not running (DOWN)" in msg
    assert "guest vm 9001 is stopped" in msg
    assert "Wake before backup" in msg
    assert "NOT a restore candidate" in msg
    assert example_session.exec(select(BackupRecord)).all() == []
    run = example_session.exec(select(PipelineRun)).one()
    assert run.status is PipelineStatus.FAILED
    names = [s["name"] for s in run.steps]
    assert "vzdump_started" not in names
    gate = next(s for s in run.steps if s["name"] == "integrity_gate")
    assert gate["detail"]["verdict"] == "down"
    assert gate["detail"]["probe_results"][0]["kind"] == "guest"


# ---------------------------------------------------------------------------
# Wake: DOWN means "not yet", not "give up"
# ---------------------------------------------------------------------------


class BootingProxmox(CountingProxmox):
    """The guest reports stopped for a few reads after start_guest, then runs."""

    def __init__(self, *, stopped_reads: int, **kw: Any) -> None:
        super().__init__(**kw)
        self.stopped_reads = stopped_reads

    async def list_guests(self, node: str | None = None) -> list[Guest]:
        self.guest_calls += 1
        status = "stopped" if self.guest_calls <= self.stopped_reads else "running"
        return [_guest(g.vmid, status, node=g.node) for g in self.simulated_guests]


async def test_wait_healthy_keeps_waiting_through_down(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    config_module.reset_settings_cache()
    node = Node(name="n1", always_on=True, power_mgr_target="n1")
    session.add(node)
    session.commit()
    session.refresh(node)
    svc = _service(session, node, "web")
    # Read 1: _ensure_guest_running (stopped -> start). Reads 2-4: three DOWN
    # scans while the guest boots. Read 5: running -> HEALTHY.
    proxmox = BootingProxmox(stopped_reads=4, nodes={"n1": True}, guests=[_guest(101, "stopped")])
    from orchestrator.adapters.power.dry_run import DryRunPowerAdapter

    settings = get_settings()
    pipeline = WakePipeline(
        power=DryRunPowerAdapter(), proxmox=proxmox, settings=settings, health=_engine(proxmox)
    )

    run = await pipeline.run_for_service(session, svc.slug, timeout_s=30)

    assert run.status is PipelineStatus.SUCCEEDED
    assert proxmox.guest_calls == 5
    state = session.get(ServiceState, svc.id)
    assert state is not None and state.last_verdict is HealthState.HEALTHY


def test_wake_and_backup_default_engines_carry_proxmox() -> None:
    from orchestrator.adapters.power.dry_run import DryRunPowerAdapter

    proxmox = DryRunProxmoxAdapter()
    wake = WakePipeline(power=DryRunPowerAdapter(), proxmox=proxmox, settings=get_settings())
    backup = BackupPipeline(proxmox=proxmox, pbs=DryRunPbsAdapter(), settings=get_settings())
    assert wake._health._proxmox is proxmox
    assert backup._health._proxmox is proxmox


# ---------------------------------------------------------------------------
# UI + API
# ---------------------------------------------------------------------------


def _seed_down(session: Session) -> Service:
    node = _node(session, "storage")
    svc = _service(session, node, "nas", guest_id=9001)
    session.add(
        ServiceState(
            service_id=svc.id,
            last_verdict=HealthState.DOWN,
            last_verdict_at=datetime.now(UTC),
            notes="guest vm 9001 is stopped — probes not run.",
            last_probe_results=[
                {
                    "probe_name": "guest",
                    "kind": "guest",
                    "state": "down",
                    "message": "guest vm 9001 is stopped",
                    "details": {
                        "guest_gate": True,
                        "node": "storage",
                        "node_online": True,
                        "guest_kind": "vm",
                        "guest_id": 9001,
                        "guest_status": "stopped",
                    },
                },
                {
                    "probe_name": "nas-p0",
                    "kind": "systemd",
                    "state": "unknown",
                    "message": "skipped — guest stopped",
                    "details": {"skipped": True, "skipped_because": "guest_down"},
                },
            ],
        )
    )
    session.add(
        ProxyHost(hostname="nas.example.lan", upstream="http://10.0.0.5", service_id=svc.id)
    )
    session.commit()
    return svc


def test_series_hosts_include_service_state(session: Session) -> None:
    _seed_down(session)
    data = build_series(session, range_key="1h")
    (host,) = data["hosts"]
    assert host["service_state"] == "down"
    assert "stopped" in host["service_state_reason"]


def _login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(*_a: Any, **_k: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=b"cred-1", credential_public_key=b"pub", sign_count=0, aaguid=""
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)
    b64 = webauthn.helpers.bytes_to_base64url(b"cred-1")
    r = client.post("/api/auth/register/begin", json={"username": "storm", "invite_token": None})
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/auth/register/complete",
        json={
            "username": "storm",
            "device_label": "t",
            "invite_token": None,
            "credential": {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}},
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    # No background scan may overwrite the seeded DOWN state.
    monkeypatch.setenv("HEALTH_SCAN_ENABLED", "false")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        _seed_down(s)
    with TestClient(create_app()) as c:
        _login(c, monkeypatch)
        yield c


def test_dashboard_renders_down_badge(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    rows = {r["slug"]: r for r in client.get("/api/views/services").json()["rows"]}
    nas = rows["nas"]
    # Off on purpose, never failed: its guest is stopped.
    assert nas["state"] == {"cls": "off", "label": "stopped", "why": "its guest is stopped"}
    assert nas["runs_on"]["sub"].endswith("stopped")
    assert nas["check"]["text"] == "not checked while off"


def test_services_list_shows_guest_status_and_wake_on_hit(client: TestClient) -> None:
    def rows() -> dict[str, Any]:
        return {r["slug"]: r for r in client.get("/api/views/services").json()["rows"]}

    assert rows()["nas"]["runs_on"]["sub"] == "vm 9001 · stopped"

    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == "nas")).one()
        svc.wake_on_hit_enabled = False
        svc.wake_trigger = WakeTrigger.BUTTON
        s.add(svc)
        s.commit()
    nas = rows()["nas"]
    assert (
        nas["wake_on_hit"] == "no"
        and nas["wake_on_hit_why"] == "wake on hit is off for this service"
    )

    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == "nas")).one()
        svc.wake_on_hit_enabled = True
        s.add(svc)
        s.commit()
    assert rows()["nas"]["wake_on_hit"] == "yes (button)"


def test_service_page_shows_guest_gate_and_skipped_probes(client: TestClient) -> None:
    r = client.get("/services/nas")
    assert r.status_code == 200, r.text
    # The last scan, guest gate included, is in the page for the Health card.
    assert '"guest": {' in r.text and '"guest_state": "stopped"' in r.text
    assert "storage" in r.text
    assert '"state": "skipped"' in r.text  # each probe's last result
    assert '"label": "stopped"' in r.text  # off on purpose: off, never failed
    js = client.get("/static/service.js").text
    assert 'name: "guest gate"' in js


def test_series_api_includes_service_state(client: TestClient) -> None:
    r = client.get("/api/traffic/series?range=1h")
    assert r.status_code == 200, r.text
    assert r.json()["hosts"][0]["service_state"] == "down"


def test_traffic_js_flags_down_hosts(client: TestClient) -> None:
    r = client.get("/static/traffic.js")
    assert r.status_code == 200
    assert 'service_state === "down"' in r.text
    assert "requests to it are failing" in r.text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_scan_prints_guest_gate_first(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import cli as cli_module

    svc = _service(session, _node(session), "web")
    assert svc.id is not None
    proxmox = CountingProxmox(nodes={"n1": True}, guests=[_guest(101, "stopped")])
    monkeypatch.setattr(cli_module, "_bootstrap", lambda: None)
    monkeypatch.setattr(
        cli_module, "build_adapters", lambda *_a, **_k: SimpleNamespace(proxmox=proxmox)
    )

    result = CliRunner().invoke(cli_module.cli, ["scan", "web"])

    assert result.exit_code == 1, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("web") and "DOWN (guest not running)" in lines[0]
    assert lines[1].strip() == "guest gate: DOWN — guest vm 101 is stopped"
    assert "skip web-p0 [systemd] — skipped — guest stopped" in result.output
