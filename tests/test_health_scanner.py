"""Periodic health scanning: the scheduler, the shared-cache consistency
guarantee, pruning, the history endpoint, and the dashboard auto-refresh.

Conventions follow ``tests/test_traffic_metrics.py`` (the most recent,
closest precedent: a periodic collector + pruning + settings gating) and
``tests/test_health_engine.py`` (the gateway/dependents fixtures and the
existing ``test_dependency_is_scanned_once_per_run`` this module's key test
extends from a single ``engine.scan`` call tree to a whole scan cycle).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import webauthn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.db import session as session_module
from orchestrator.db.models import HealthScanSample, Node, Service
from orchestrator.db.models import Probe as ProbeRow
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, HealthState, ProbeKind
from orchestrator.health.engine import HealthEngine, TransportFactory
from orchestrator.health.transports.dry_run import DryRunTransport
from orchestrator.scheduler.health_scanner import HealthScanner
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FixedTransportFactory(TransportFactory):
    """Always hands back one transport, whatever the spec says — same helper
    as test_health_engine.py's, duplicated locally to avoid a cross-test-file
    import."""

    def __init__(self, transport: DryRunTransport) -> None:
        self._t = transport

    def build(self, spec: Any) -> DryRunTransport:
        return self._t


def _scanner(transport: DryRunTransport | None = None) -> HealthScanner:
    scanner = HealthScanner(settings=config_module.get_settings())
    scanner._engine = HealthEngine(
        settings=config_module.get_settings(),
        transport_factory=_FixedTransportFactory(transport or DryRunTransport()),
    )
    return scanner


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
    guest_id: int,
    enabled: bool = True,
    depends_on: list[str] | None = None,
) -> Service:
    svc = Service(
        slug=slug,
        name=slug,
        node_id=node.id,
        guest_kind=GuestKind.VM,
        guest_id=guest_id,
        enabled=enabled,
        depends_on=depends_on or [],
    )
    session.add(svc)
    session.commit()
    session.refresh(svc)
    return svc


def _probe(service: Service, *, name: str, unit: str) -> ProbeRow:
    return ProbeRow(
        service_id=service.id,
        name=name,
        kind=ProbeKind.SYSTEMD,
        required=True,
        config={"unit": unit},
    )


# ---------------------------------------------------------------------------
# start()/HEALTH_SCAN_ENABLED gating
# ---------------------------------------------------------------------------


def test_scanner_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTH_SCAN_ENABLED", "false")
    config_module.reset_settings_cache()

    scanner = HealthScanner(settings=config_module.get_settings())
    scanner.start()  # must not raise, and must not schedule a job
    assert scanner.running is False


async def test_scanner_starts_when_enabled() -> None:
    scanner = HealthScanner(settings=config_module.get_settings())
    try:
        scanner.start()
        assert scanner.running is True
    finally:
        await scanner.shutdown()


# ---------------------------------------------------------------------------
# One cycle populates HealthScanSample for every enabled service
# ---------------------------------------------------------------------------


async def test_poll_once_writes_a_sample_per_enabled_service(session: Session) -> None:
    node = _node(session)
    a = _service(session, node, "a", guest_id=1)
    session.add(_probe(a, name="pa", unit="a"))
    b = _service(session, node, "b", guest_id=2)
    session.add(_probe(b, name="pb", unit="b"))
    session.commit()

    inserted = await _scanner().poll_once()
    assert inserted == 2

    with Session(get_engine()) as s:
        rows = s.exec(select(HealthScanSample)).all()
    assert {r.service_id for r in rows} == {a.id, b.id}
    assert all(r.state is HealthState.HEALTHY for r in rows)


async def test_poll_once_skips_disabled_services_not_pulled_in_as_a_dependency(
    session: Session,
) -> None:
    node = _node(session)
    _service(session, node, "off", guest_id=1, enabled=False)
    on = _service(session, node, "on", guest_id=2)
    session.add(_probe(on, name="p", unit="on"))
    session.commit()

    await _scanner().poll_once()

    with Session(get_engine()) as s:
        rows = s.exec(select(HealthScanSample)).all()
    assert {r.service_id for r in rows} == {on.id}


async def test_poll_once_includes_a_disabled_service_that_is_a_live_dependency(
    session: Session,
) -> None:
    """A disabled gateway someone still ``depends_on`` is scanned (the
    dependency chain does not care about ``enabled``) and so belongs in the
    history too — leaving it out would be inconsistent with what was
    actually probed this cycle."""
    node = _node(session)
    gateway = _service(session, node, "gateway", guest_id=1, enabled=False)
    session.add(_probe(gateway, name="gw", unit="gw"))
    downstream = _service(session, node, "svc", guest_id=2, depends_on=["gateway"])
    session.commit()

    await _scanner().poll_once()

    with Session(get_engine()) as s:
        rows = s.exec(select(HealthScanSample)).all()
    assert {r.service_id for r in rows} == {gateway.id, downstream.id}


# ---------------------------------------------------------------------------
# THE regression test: a shared gateway is probed exactly once per cycle,
# and every dependent — plus the gateway's own row — agrees about it.
# ---------------------------------------------------------------------------


async def test_shared_gateway_is_probed_once_per_cycle_and_verdicts_agree(
    session: Session,
) -> None:
    """Two services depending on the same gateway, scanned in one cycle,
    must not re-probe the gateway per dependent (the original bug: each
    top-level call used to start its own fresh cache, so a shared dependency
    could be probed once per caller and — on a flaky/slow network path —
    answer differently each time, leaving mutually inconsistent persisted
    verdicts). This asserts BOTH halves of the fix: the probe count AND that
    every row ends up with the identical verdict/timestamp for the gateway.
    """
    node = _node(session)
    gateway = _service(session, node, "gateway", guest_id=1)
    session.add(_probe(gateway, name="gw", unit="gw"))
    for slug, guest_id in (("a", 2), ("b", 3)):
        svc = _service(session, node, slug, guest_id=guest_id, depends_on=["gateway"])
        session.add(_probe(svc, name=f"p-{slug}", unit=slug))
    session.commit()

    transport = DryRunTransport()
    scanner = _scanner(transport)
    inserted = await scanner.poll_once()
    assert inserted == 3

    # The gateway's own unit was checked exactly once this cycle, no matter
    # how many services depend on it.
    gateway_calls = [c for c in transport.calls if c[-1] == "gw"]
    assert len(gateway_calls) == 1

    # And both dependents got judged HEALTHY off that SAME probe — i.e.
    # neither was blocked by a gateway verdict that disagreed with the
    # gateway's own persisted row.
    with Session(get_engine()) as s:
        rows = {r.service_id: r for r in s.exec(select(HealthScanSample)).all()}
    gateway_row = rows[gateway.id]
    assert gateway_row.state is HealthState.HEALTHY
    for slug_id in (rows[s_id] for s_id in rows if s_id != gateway.id):
        assert slug_id.state is HealthState.HEALTHY
        # Same cycle => same timestamp (they were all written from the one
        # cache built during this single poll_once() call).
        assert slug_id.sampled_at == gateway_row.sampled_at


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


async def test_prune_removes_samples_older_than_retention(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEALTH_HISTORY_RETENTION_DAYS", "1")
    config_module.reset_settings_cache()

    node = _node(session)
    svc = _service(session, node, "svc", guest_id=1)
    session.add(
        HealthScanSample(
            service_id=svc.id,
            sampled_at=datetime.now(UTC) - timedelta(days=10),
            state=HealthState.HEALTHY,
            reason="old",
        )
    )
    session.commit()

    await _scanner().poll_once()

    with Session(get_engine()) as s:
        reasons = [r.reason for r in s.exec(select(HealthScanSample)).all()]
    assert "old" not in reasons


# ---------------------------------------------------------------------------
# A crashing probe for one service must not stop the rest of the cycle
# ---------------------------------------------------------------------------


async def test_crashing_probe_does_not_stop_the_rest_of_the_cycle(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _node(session)
    bad = _service(session, node, "bad", guest_id=1)
    session.add(_probe(bad, name="pb", unit="bad"))
    good = _service(session, node, "good", guest_id=2)
    session.add(_probe(good, name="pg", unit="good"))
    session.commit()

    scanner = _scanner()
    real_scan = scanner._engine.scan

    async def _boom(session_arg: Session, service: Service, **kwargs: Any) -> Any:
        if service.slug == "bad":
            raise RuntimeError("probe implementation exploded")
        return await real_scan(session_arg, service, **kwargs)

    monkeypatch.setattr(scanner._engine, "scan", _boom)

    inserted = await scanner.poll_once()
    assert inserted == 1  # only "good" made it into a sample

    with Session(get_engine()) as s:
        rows = s.exec(select(HealthScanSample)).all()
    assert {r.service_id for r in rows} == {good.id}


# ---------------------------------------------------------------------------
# Health-history API endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _fake_verify_registration(monkeypatch: pytest.MonkeyPatch, credential_id: bytes) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"pub-" + credential_id,
            sign_count=0,
            aaguid="",
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)


def _credential(raw_id: bytes) -> dict[str, Any]:
    b64 = webauthn.helpers.bytes_to_base64url(raw_id)
    return {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}}


def _login_as_first_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_verify_registration(monkeypatch, b"cred-1")
    r = client.post("/api/auth/register/begin", json={"username": "storm", "invite_token": None})
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/auth/register/complete",
        json={
            "username": "storm",
            "device_label": "test-device",
            "invite_token": None,
            "credential": _credential(b"cred-1"),
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def client(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(app) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_health_history_endpoint_returns_oldest_first(client: TestClient) -> None:
    with Session(get_engine()) as s:
        node = _node(s)
        svc = _service(s, node, "svc", guest_id=1)
        base = datetime.now(UTC) - timedelta(minutes=30)
        for i, state in enumerate((HealthState.FAILED, HealthState.UNKNOWN, HealthState.HEALTHY)):
            s.add(
                HealthScanSample(
                    service_id=svc.id,
                    sampled_at=base + timedelta(minutes=10 * i),
                    state=state,
                    reason=f"reason-{i}",
                )
            )
        s.commit()

    r = client.get("/api/registry/services/svc/health-history")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["state"] for row in rows] == ["failed", "unknown", "healthy"]
    assert rows[0]["reason"] == "reason-0"


def test_health_history_endpoint_unknown_service_is_404(client: TestClient) -> None:
    r = client.get("/api/registry/services/does-not-exist/health-history")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Dashboard / service page render the auto-refresh script
# ---------------------------------------------------------------------------


def test_dashboard_page_has_autorefresh_script(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200, r.text
    assert "setInterval" in r.text
    assert "location.reload" in r.text


def test_service_detail_page_has_autorefresh_script(client: TestClient) -> None:
    with Session(get_engine()) as s:
        node = _node(s)
        _service(s, node, "svc", guest_id=1)

    r = client.get("/services/svc")
    assert r.status_code == 200, r.text
    assert "setInterval" in r.text
    assert "location.reload" in r.text
    # The guard that keeps the refresh from clobbering an in-progress edit.
    assert "shouldSkipAutoReload" in r.text
