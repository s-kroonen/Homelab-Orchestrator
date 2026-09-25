"""Health engine: probe execution, the three-state mapping, and aggregation.

The aggregation rules and the transport-error mapping are the load-bearing parts.
Get them wrong and either a corrupt service gets backed up, or a network blip
gets reported as corruption.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
import pytest
from sqlmodel import Session, select

from orchestrator.db.models import Node, Service, ServiceState
from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import GuestKind, HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult
from orchestrator.health.engine import HealthEngine, TransportFactory, _spec_from_config
from orchestrator.health.probes import get_probe_class, registered_kinds
from orchestrator.health.transports.base import (
    CommandResult,
    TransportConfigError,
    TransportUnreachable,
)
from orchestrator.health.transports.dry_run import DryRunTransport

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _probe(
    service: Service,
    kind: ProbeKind,
    *,
    name: str = "p",
    required: bool = True,
    config: dict | None = None,
    timeout_s: int = 5,
) -> ProbeRow:
    return ProbeRow(
        service_id=service.id,
        name=name,
        kind=kind,
        required=required,
        timeout_s=timeout_s,
        config=config or {},
    )


class _FixedTransportFactory(TransportFactory):
    """Always hands back one transport, whatever the spec says."""

    def __init__(self, transport):
        self._t = transport

    def build(self, spec):
        return self._t


def _engine(transport=None) -> HealthEngine:
    factory = _FixedTransportFactory(transport or DryRunTransport())
    return HealthEngine(transport_factory=factory)


# ---------------------------------------------------------------------------
# Aggregation — the rule that decides whether a backup happens
# ---------------------------------------------------------------------------


def test_all_required_healthy_is_healthy(session: Session, service: Service) -> None:
    rows = [_probe(service, ProbeKind.HTTP, name="a"), _probe(service, ProbeKind.HTTP, name="b")]
    results = [
        ProbeResult(probe_name="a", kind=ProbeKind.HTTP, state=HealthState.HEALTHY),
        ProbeResult(probe_name="b", kind=ProbeKind.HTTP, state=HealthState.HEALTHY),
    ]
    assert _engine().aggregate(rows, results).state is HealthState.HEALTHY


def test_any_required_failed_is_failed(session: Session, service: Service) -> None:
    rows = [_probe(service, ProbeKind.HTTP, name="a"), _probe(service, ProbeKind.HTTP, name="b")]
    results = [
        ProbeResult(probe_name="a", kind=ProbeKind.HTTP, state=HealthState.HEALTHY),
        ProbeResult(probe_name="b", kind=ProbeKind.HTTP, state=HealthState.FAILED),
    ]
    assert _engine().aggregate(rows, results).state is HealthState.FAILED


def test_failed_beats_unknown(session: Session, service: Service) -> None:
    """A definite negative outranks an indeterminate one — if one probe knows the
    service is broken, the fact another probe couldn't tell doesn't soften it."""
    rows = [_probe(service, ProbeKind.HTTP, name="a"), _probe(service, ProbeKind.HTTP, name="b")]
    results = [
        ProbeResult(probe_name="a", kind=ProbeKind.HTTP, state=HealthState.UNKNOWN),
        ProbeResult(probe_name="b", kind=ProbeKind.HTTP, state=HealthState.FAILED),
    ]
    assert _engine().aggregate(rows, results).state is HealthState.FAILED


def test_unknown_when_any_required_is_indeterminate(session: Session, service: Service) -> None:
    rows = [_probe(service, ProbeKind.HTTP, name="a"), _probe(service, ProbeKind.HTTP, name="b")]
    results = [
        ProbeResult(probe_name="a", kind=ProbeKind.HTTP, state=HealthState.HEALTHY),
        ProbeResult(probe_name="b", kind=ProbeKind.HTTP, state=HealthState.UNKNOWN),
    ]
    assert _engine().aggregate(rows, results).state is HealthState.UNKNOWN


def test_no_required_probes_is_unknown_not_healthy(session: Session, service: Service) -> None:
    """The most important rule in the engine.

    A service nobody wrote a check for has NOT been verified. Treating that as
    healthy would open the gate widest on exactly the services least understood,
    and would make forgetting to configure a probe silently permissive.
    """
    rows = [_probe(service, ProbeKind.HTTP, name="diag", required=False)]
    results = [ProbeResult(probe_name="diag", kind=ProbeKind.HTTP, state=HealthState.HEALTHY)]
    verdict = _engine().aggregate(rows, results)
    assert verdict.state is HealthState.UNKNOWN
    assert "no required probes" in verdict.reason


def test_empty_probe_list_is_unknown(session: Session, service: Service) -> None:
    verdict = _engine().aggregate([], [])
    assert verdict.state is HealthState.UNKNOWN


def test_non_required_failure_does_not_block(session: Session, service: Service) -> None:
    """Diagnostic-only probes are recorded but must not gate a backup."""
    rows = [
        _probe(service, ProbeKind.HTTP, name="req"),
        _probe(service, ProbeKind.HTTP, name="diag", required=False),
    ]
    results = [
        ProbeResult(probe_name="req", kind=ProbeKind.HTTP, state=HealthState.HEALTHY),
        ProbeResult(probe_name="diag", kind=ProbeKind.HTTP, state=HealthState.FAILED),
    ]
    verdict = _engine().aggregate(rows, results)
    assert verdict.state is HealthState.HEALTHY
    assert len(verdict.probe_results) == 2  # still recorded for the dashboard


# ---------------------------------------------------------------------------
# Transport errors must map to UNKNOWN, never FAILED
# ---------------------------------------------------------------------------


async def test_unreachable_transport_is_unknown_not_failed(
    session: Session, service: Service
) -> None:
    """An SSH refusal tells us nothing about the service. Calling it FAILED would
    make a network problem look like corruption and flag a restore."""
    row = _probe(service, ProbeKind.SYSTEMD, config={"unit": "x.service"})
    session.add(row)
    session.commit()

    engine = _engine(DryRunTransport(unreachable=True))
    verdict = await engine.scan(session, service)

    assert verdict.state is HealthState.UNKNOWN
    assert verdict.probe_results[0].details["indeterminate"] is True


async def test_nonzero_exit_is_failed(session: Session, service: Service) -> None:
    """The command ran and said no — that IS a verdict about the service."""
    row = _probe(service, ProbeKind.SYSTEMD, config={"unit": "x.service"})
    session.add(row)
    session.commit()

    transport = DryRunTransport(default_result=CommandResult(exit_code=3, stderr="inactive"))
    verdict = await _engine(transport).scan(session, service)

    assert verdict.state is HealthState.FAILED


async def test_missing_transport_config_is_unknown(session: Session, service: Service) -> None:
    """A misconfigured probe must not read as a broken service."""

    class BrokenFactory(TransportFactory):
        def __init__(self) -> None:
            pass

        def build(self, spec):
            raise TransportConfigError("ssh transport requires a host")

    row = _probe(service, ProbeKind.SYSTEMD, config={"unit": "x.service"})
    session.add(row)
    session.commit()

    engine = HealthEngine(transport_factory=BrokenFactory())
    verdict = await engine.scan(session, service)
    assert verdict.state is HealthState.UNKNOWN
    assert verdict.probe_results[0].details["config_error"] is True


async def test_probe_crash_is_unknown_not_failed(session: Session, service: Service) -> None:
    """A bug in our probe code is our problem, not a verdict about their data."""
    row = _probe(service, ProbeKind.SYSTEMD, config={})  # no 'unit' key -> KeyError
    session.add(row)
    session.commit()

    verdict = await _engine().scan(session, service)
    assert verdict.state is HealthState.UNKNOWN


# ---------------------------------------------------------------------------
# Probes: HTTP state mapping
# ---------------------------------------------------------------------------


async def test_http_5xx_is_failed_but_refused_connection_is_unknown(
    session: Session, service: Service, monkeypatch
) -> None:
    """The core HTTP distinction: a 500 is an answer, a refusal is not."""
    row = _probe(service, ProbeKind.HTTP, config={"url": "http://x.invalid/"})
    probe = get_probe_class(ProbeKind.HTTP)()

    class FakeClient:
        def __init__(self, *a, **k) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def request(self, *a, **k):
            return httpx.Response(500, text="boom")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert (await probe.run(row)).state is HealthState.FAILED

    class RefusingClient(FakeClient):
        async def request(self, *a, **k):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", RefusingClient)
    assert (await probe.run(row)).state is HealthState.UNKNOWN


async def test_http_body_assertion_failure_is_failed(
    session: Session, service: Service, monkeypatch
) -> None:
    row = _probe(
        service,
        ProbeKind.HTTP,
        config={"url": "http://x.invalid/", "expect_body_contains": "installed"},
    )

    class FakeClient:
        def __init__(self, *a, **k) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def request(self, *a, **k):
            return httpx.Response(200, text="something else")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    result = await get_probe_class(ProbeKind.HTTP)().run(row)
    assert result.state is HealthState.FAILED
    assert "did not contain" in result.message


# ---------------------------------------------------------------------------
# Probes: docker project
# ---------------------------------------------------------------------------


async def test_docker_project_all_running_is_healthy(session: Session, service: Service) -> None:
    row = _probe(service, ProbeKind.DOCKER_PROJECT, config={"project": "stack"})
    transport = DryRunTransport(
        default_result=CommandResult(
            exit_code=0, stdout="web\trunning\tUp 2 hours\ndb\trunning\tUp 2 hours (healthy)\n"
        )
    )
    result = await get_probe_class(ProbeKind.DOCKER_PROJECT)().run(row, transport=transport)
    assert result.state is HealthState.HEALTHY


async def test_docker_project_unhealthy_container_is_failed(
    session: Session, service: Service
) -> None:
    row = _probe(service, ProbeKind.DOCKER_PROJECT, config={"project": "stack"})
    transport = DryRunTransport(
        default_result=CommandResult(
            exit_code=0, stdout="web\trunning\tUp (unhealthy)\ndb\trunning\tUp 2 hours\n"
        )
    )
    result = await get_probe_class(ProbeKind.DOCKER_PROJECT)().run(row, transport=transport)
    assert result.state is HealthState.FAILED
    assert "unhealthy" in result.message


async def test_docker_project_exited_container_is_failed(
    session: Session, service: Service
) -> None:
    row = _probe(service, ProbeKind.DOCKER_PROJECT, config={"project": "stack"})
    transport = DryRunTransport(
        default_result=CommandResult(exit_code=0, stdout="web\texited\tExited (1) 3 min ago\n")
    )
    result = await get_probe_class(ProbeKind.DOCKER_PROJECT)().run(row, transport=transport)
    assert result.state is HealthState.FAILED


async def test_docker_query_failure_is_unknown(session: Session, service: Service) -> None:
    """If docker itself won't answer we know nothing about the containers."""
    row = _probe(service, ProbeKind.DOCKER_PROJECT, config={"project": "stack"})
    transport = DryRunTransport(
        default_result=CommandResult(exit_code=1, stderr="permission denied on docker.sock")
    )
    result = await get_probe_class(ProbeKind.DOCKER_PROJECT)().run(row, transport=transport)
    assert result.state is HealthState.UNKNOWN


# ---------------------------------------------------------------------------
# Probes: ansible exit-code semantics
# ---------------------------------------------------------------------------


async def test_ansible_unreachable_exit_4_is_unknown(session: Session, service: Service) -> None:
    """Ansible exit 4 means it could not reach the host — indeterminate."""
    row = _probe(service, ProbeKind.ANSIBLE_PLAYBOOK, config={"playbook": __file__})
    transport = DryRunTransport(default_result=CommandResult(exit_code=4, stderr="UNREACHABLE"))
    result = await get_probe_class(ProbeKind.ANSIBLE_PLAYBOOK)().run(row, transport=transport)
    assert result.state is HealthState.UNKNOWN


async def test_ansible_task_failure_exit_2_is_failed(session: Session, service: Service) -> None:
    row = _probe(service, ProbeKind.ANSIBLE_PLAYBOOK, config={"playbook": __file__})
    transport = DryRunTransport(default_result=CommandResult(exit_code=2, stderr="FAILED!"))
    result = await get_probe_class(ProbeKind.ANSIBLE_PLAYBOOK)().run(row, transport=transport)
    assert result.state is HealthState.FAILED


async def test_ansible_missing_playbook_is_unknown_with_mount_hint(
    session: Session, service: Service
) -> None:
    row = _probe(service, ProbeKind.ANSIBLE_PLAYBOOK, config={"playbook": "/nope/missing.yml"})
    result = await get_probe_class(ProbeKind.ANSIBLE_PLAYBOOK)().run(
        row, transport=DryRunTransport()
    )
    assert result.state is HealthState.UNKNOWN
    assert "mounted" in result.message  # points at the real cause in Docker


# ---------------------------------------------------------------------------
# Command safety + config plumbing
# ---------------------------------------------------------------------------


async def test_command_probe_rejects_a_shell_string(session: Session, service: Service) -> None:
    """argv only. A shell string in an operator-editable registry is an
    injection surface, so it is refused with an explanation."""
    row = _probe(service, ProbeKind.COMMAND, config={"argv": "rm -rf /"})
    result = await get_probe_class(ProbeKind.COMMAND)().run(row, transport=DryRunTransport())
    assert result.state is HealthState.UNKNOWN
    assert "list of strings" in result.message


def test_transport_spec_defaults_to_local_without_a_block() -> None:
    assert _spec_from_config({}).type == "local"


def test_transport_spec_reads_ssh_block() -> None:
    spec = _spec_from_config(
        {"transport": {"type": "ssh", "host": "h", "user": "u", "port": 2222, "key_path": "/k"}}
    )
    assert (spec.type, spec.host, spec.user, spec.port) == ("ssh", "h", "u", 2222)
    assert spec.extra["key_path"] == "/k"


def test_every_probe_kind_has_an_implementation() -> None:
    """A ProbeKind with no implementation would silently return UNKNOWN forever."""
    from orchestrator.domain.enums import ProbeKind as PK

    missing = {k.value for k in PK} - {k.value for k in registered_kinds()}
    assert not missing, f"probe kinds with no implementation: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_scan_persists_verdict_to_service_state(session: Session, service: Service) -> None:
    """The dashboard and the gate must read the same verdict."""
    row = _probe(service, ProbeKind.SYSTEMD, config={"unit": "x.service"})
    session.add(row)
    session.commit()

    await _engine().scan(session, service)

    state = session.exec(select(ServiceState).where(ServiceState.service_id == service.id)).one()
    assert state.last_verdict is HealthState.HEALTHY
    assert state.last_verdict_at is not None
    assert len(state.last_probe_results) == 1
    assert state.notes  # carries the reason


async def test_rescan_overwrites_previous_verdict(session: Session, service: Service) -> None:
    row = _probe(service, ProbeKind.SYSTEMD, config={"unit": "x.service"})
    session.add(row)
    session.commit()

    await _engine().scan(session, service)
    await _engine(DryRunTransport(default_result=CommandResult(exit_code=3))).scan(session, service)

    state = session.exec(select(ServiceState).where(ServiceState.service_id == service.id)).one()
    assert state.last_verdict is HealthState.FAILED


async def test_unreachable_transport_raises_the_right_type() -> None:
    """Guards the contract the whole three-state model rests on."""
    with pytest.raises(TransportUnreachable):
        await DryRunTransport(unreachable=True).run(["true"], timeout_s=1)


# ---------------------------------------------------------------------------
# Dependencies — the gateway case
#
# The orchestrator is not on the guest network, so every probe reaches guests
# through a gateway. When that gateway is down, one outage must not look like
# every service breaking at once.
# ---------------------------------------------------------------------------


def _service(session: Session, slug: str, *, depends_on: list[str] | None = None) -> Service:
    node = session.exec(select(Node).where(Node.name == "eve1")).one_or_none()
    if node is None:
        node = Node(name="eve1", always_on=True)
        session.add(node)
        session.commit()
        session.refresh(node)
    svc = Service(
        slug=slug,
        name=slug,
        node_id=node.id,
        guest_kind=GuestKind.VM,
        guest_id=100,
        depends_on=depends_on or [],
    )
    session.add(svc)
    session.commit()
    session.refresh(svc)
    return svc


async def test_downstream_is_unknown_when_the_gateway_is_failed(session: Session) -> None:
    """A FAILED gateway must NOT make downstream services FAILED.

    FAILED marks a service as a restore candidate. Inheriting that from the
    gateway would propose restoring services whose data was never inspected.
    """
    gateway = _service(session, "gateway")
    session.add(_probe(gateway, ProbeKind.SYSTEMD, name="gw", config={"unit": "x"}))
    downstream = _service(session, "haos", depends_on=["gateway"])
    session.add(_probe(downstream, ProbeKind.SYSTEMD, name="svc", config={"unit": "y"}))
    session.commit()

    # exit 3 => the gateway's own probe FAILS
    engine = _engine(DryRunTransport(default_result=CommandResult(exit_code=3)))
    verdict = await engine.scan(session, downstream)

    assert verdict.state is HealthState.UNKNOWN  # not FAILED
    assert "gateway" in verdict.reason
    assert "NOT evidence" in verdict.reason


async def test_downstream_probes_do_not_run_when_blocked(session: Session) -> None:
    """Short-circuit rather than firing probes guaranteed to time out.

    With 27 services behind one gateway, running them anyway means 27 timeouts
    and 27 misleading failures describing the wrong problem.
    """
    gateway = _service(session, "gateway")
    session.add(_probe(gateway, ProbeKind.SYSTEMD, name="gw", config={"unit": "x"}))
    downstream = _service(session, "haos", depends_on=["gateway"])
    session.add(_probe(downstream, ProbeKind.SYSTEMD, name="svc", config={"unit": "y"}))
    session.commit()

    transport = DryRunTransport(default_result=CommandResult(exit_code=3))
    await _engine(transport).scan(session, downstream)

    # Only the gateway probe ran.
    assert len(transport.calls) == 1
    assert transport.calls[0][-1] == "x"


async def test_healthy_gateway_lets_downstream_be_judged(session: Session) -> None:
    gateway = _service(session, "gateway")
    session.add(_probe(gateway, ProbeKind.SYSTEMD, name="gw", config={"unit": "x"}))
    downstream = _service(session, "haos", depends_on=["gateway"])
    session.add(_probe(downstream, ProbeKind.SYSTEMD, name="svc", config={"unit": "y"}))
    session.commit()

    verdict = await _engine().scan(session, downstream)  # dry-run exits 0
    assert verdict.state is HealthState.HEALTHY


async def test_dependency_is_scanned_once_per_run(session: Session) -> None:
    """Two services behind one gateway must probe the gateway once, not twice."""
    gateway = _service(session, "gateway")
    session.add(_probe(gateway, ProbeKind.SYSTEMD, name="gw", config={"unit": "gw"}))
    for slug in ("a", "b"):
        svc = _service(session, slug, depends_on=["gateway"])
        session.add(_probe(svc, ProbeKind.SYSTEMD, name=f"p-{slug}", config={"unit": slug}))
    session.commit()

    transport = DryRunTransport()
    engine = _engine(transport)
    cache: dict = {}
    for slug in ("a", "b"):
        svc = session.exec(select(Service).where(Service.slug == slug)).one()
        await engine.scan(session, svc, _cache=cache)

    gateway_calls = [c for c in transport.calls if c[-1] == "gw"]
    assert len(gateway_calls) == 1


async def test_unknown_gateway_also_blocks(session: Session) -> None:
    """An unreachable gateway is the commonest case and must block too."""
    gateway = _service(session, "gateway")
    session.add(_probe(gateway, ProbeKind.SYSTEMD, name="gw", config={"unit": "x"}))
    downstream = _service(session, "haos", depends_on=["gateway"])
    session.commit()

    verdict = await _engine(DryRunTransport(unreachable=True)).scan(session, downstream)
    assert verdict.state is HealthState.UNKNOWN
    assert "gateway" in verdict.reason


async def test_missing_dependency_is_unknown_not_a_crash(session: Session) -> None:
    downstream = _service(session, "haos", depends_on=["not-in-registry"])
    session.commit()
    verdict = await _engine().scan(session, downstream)
    assert verdict.state is HealthState.UNKNOWN
    assert "not in the registry" in verdict.reason


async def test_dependency_cycle_in_the_db_does_not_recurse_forever(session: Session) -> None:
    """validate_references blocks cycles in the FILE; this guards the DB path."""
    a = _service(session, "a", depends_on=["b"])
    _service(session, "b", depends_on=["a"])
    session.commit()

    verdict = await _engine().scan(session, a)
    assert verdict.state is HealthState.UNKNOWN
    assert "cycle" in verdict.reason


async def test_blocked_verdict_is_persisted(session: Session) -> None:
    """The dashboard must show the block, not a stale verdict."""
    gateway = _service(session, "gateway")
    session.add(_probe(gateway, ProbeKind.SYSTEMD, name="gw", config={"unit": "x"}))
    downstream = _service(session, "haos", depends_on=["gateway"])
    session.commit()

    await _engine(DryRunTransport(unreachable=True)).scan(session, downstream)
    state = session.exec(select(ServiceState).where(ServiceState.service_id == downstream.id)).one()
    assert state.last_verdict is HealthState.UNKNOWN
    assert "gateway" in state.notes


# ---------------------------------------------------------------------------
# HTTP through a proxy by local IP — curl --resolve semantics
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Captures what the probe actually put on the wire."""

    seen: ClassVar[dict] = {}

    def __init__(self, *a, **k) -> None: ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a) -> None:
        return None

    async def request(self, method, url, headers=None, extensions=None, **k):
        type(self).seen = {
            "url": url,
            "headers": headers or {},
            "extensions": extensions,
        }
        return httpx.Response(200, text="ok")


async def test_http_sends_host_header_and_sni(
    session: Session, service: Service, monkeypatch
) -> None:
    """Reach the proxy by IP, route by name, keep TLS valid."""
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    row = _probe(
        service,
        ProbeKind.HTTP,
        config={"url": "https://192.168.1.10/health", "host_header": "media.example.com"},
    )
    result = await get_probe_class(ProbeKind.HTTP)().run(row)
    seen = _RecordingClient.seen

    assert result.state is HealthState.HEALTHY
    assert seen["url"] == "https://192.168.1.10/health"  # connects by IP
    assert seen["headers"]["Host"] == "media.example.com"  # routes by name
    # SNI defaults to the Host header so a real certificate still validates.
    assert seen["extensions"]["sni_hostname"] == "media.example.com"


async def test_http_sni_can_differ_from_host_header(
    session: Session, service: Service, monkeypatch
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    row = _probe(
        service,
        ProbeKind.HTTP,
        config={
            "url": "https://10.0.0.1/",
            "host_header": "a.example.com",
            "sni_hostname": "b.example.com",
        },
    )
    await get_probe_class(ProbeKind.HTTP)().run(row)
    assert _RecordingClient.seen["extensions"]["sni_hostname"] == "b.example.com"


async def test_http_without_host_header_sends_no_override(
    session: Session, service: Service, monkeypatch
) -> None:
    """The plain case must not gain a spurious Host header or SNI extension."""
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    row = _probe(service, ProbeKind.HTTP, config={"url": "https://svc.lan/"})
    await get_probe_class(ProbeKind.HTTP)().run(row)
    assert "Host" not in _RecordingClient.seen["headers"]
    assert _RecordingClient.seen["extensions"] is None
