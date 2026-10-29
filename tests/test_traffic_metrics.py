"""Traffic metrics: Prometheus text parsing, the collector, the dashboard
page, and the migration.

Traefik mocking follows ``tests/test_proxy_hosts.py``'s pattern
(``_patch_traefik_transport`` + ``httpx.MockTransport``), extended to also
serve the plain-text ``/metrics`` endpoint alongside the existing JSON ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import webauthn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.factory import build_adapters
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import (
    AuditEntry,
    Node,
    NodeState,
    ProxyHost,
    Service,
    ServiceState,
    TrafficSample,
)
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import HealthState, PowerState
from orchestrator.registry.traefik_status import RouterMetricSample, parse_router_metrics
from orchestrator.scheduler.traffic_collector import WAKE_ON_HIT_ACTOR, TrafficCollector
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app

# ---------------------------------------------------------------------------
# Prometheus text parsing
# ---------------------------------------------------------------------------


def test_parse_router_metrics_sums_codes_and_duration() -> None:
    text = """
# HELP traefik_router_requests_total count
# TYPE traefik_router_requests_total counter
traefik_router_requests_total{code="200",method="GET",protocol="http",router="my-router@docker",service="svc@docker"} 1234
traefik_router_requests_total{code="404",method="GET",protocol="http",router="my-router@docker",service="svc@docker"} 12
# TYPE traefik_router_request_duration_seconds histogram
traefik_router_request_duration_seconds_sum{router="my-router@docker"} 45.6
traefik_router_request_duration_seconds_count{router="my-router@docker"} 1246
"""
    result = parse_router_metrics(text)
    assert set(result) == {"my-router@docker"}
    sample = result["my-router@docker"]
    assert sample.requests_total == 1246
    assert sample.by_code == {"200": 1234, "404": 12}
    assert sample.duration_sum_s == pytest.approx(45.6)
    assert sample.duration_count == 1246


def test_parse_router_metrics_skips_malformed_and_unrelated_lines() -> None:
    text = """
# a plain comment
not a metric line at all
traefik_unrelated_metric{router="x"} 5
traefik_router_requests_total{router="no-code@docker"} 7
traefik_router_requests_total{code="200",router="bad@docker"} not-a-number
traefik_router_requests_total{code="200",router="good@docker"} 9
"""
    result = parse_router_metrics(text)
    # "x" is an unrelated family, "bad@docker" has a non-numeric value, and
    # the two plain-text lines don't match the metric-line shape at all —
    # none of them should take down parsing of the one good line.
    assert "x" not in result
    assert "bad@docker" not in result
    assert result["good@docker"].requests_total == 9
    # No `code` label still produces a sample, bucketed under "unknown".
    assert result["no-code@docker"].by_code == {"unknown": 7}


def test_parse_router_metrics_empty_body_is_empty_not_none() -> None:
    assert parse_router_metrics("") == {}
    assert parse_router_metrics("# just comments\n") == {}


# ---------------------------------------------------------------------------
# fixtures shared by the collector + page tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _credential(raw_id: bytes) -> dict[str, Any]:
    b64 = webauthn.helpers.bytes_to_base64url(raw_id)
    return {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}}


def _fake_verify_registration(monkeypatch: pytest.MonkeyPatch, credential_id: bytes) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"pub-" + credential_id,
            sign_count=0,
            aaguid="",
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)


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


def _patch_traefik_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import orchestrator.registry.traefik_status as ts_mod

    real_async_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_async_client):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ts_mod.httpx, "AsyncClient", _PatchedAsyncClient)


def _traefik_responder(
    *,
    routers: list[dict[str, Any]],
    metrics_text: str,
    services: list[dict[str, Any]] | None = None,
) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/http/routers":
            return httpx.Response(200, json=routers)
        if request.url.path == "/api/http/services":
            return httpx.Response(200, json=services if services is not None else [])
        if request.url.path == "/metrics":
            return httpx.Response(200, text=metrics_text, headers={"content-type": "text/plain"})
        return httpx.Response(404, json={})

    return handle


def _make_proxy_host(session: Session, service: Service, hostname: str) -> ProxyHost:
    ph = ProxyHost(hostname=hostname, service_id=service.id)
    session.add(ph)
    session.commit()
    session.refresh(ph)
    return ph


# ---------------------------------------------------------------------------
# TrafficCollector
# ---------------------------------------------------------------------------


def test_collector_inserts_sample_for_matched_router(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    with Session(get_engine()) as s:
        ph = _make_proxy_host(s, service, "a.example.com")
        ph_id = ph.id

    _patch_traefik_transport(
        monkeypatch,
        _traefik_responder(
            routers=[{"name": "r1@docker", "rule": "Host(`a.example.com`)", "status": "enabled"}],
            metrics_text=(
                'traefik_router_requests_total{code="200",router="r1@docker"} 100\n'
                'traefik_router_requests_total{code="404",router="r1@docker"} 5\n'
                'traefik_router_request_duration_seconds_sum{router="r1@docker"} 1.5\n'
                'traefik_router_request_duration_seconds_count{router="r1@docker"} 105\n'
            ),
        ),
    )

    import asyncio

    collector = TrafficCollector(settings=get_settings())
    inserted = asyncio.run(collector.poll_once())
    assert inserted == 1

    with Session(get_engine()) as s:
        rows = s.exec(select(TrafficSample).where(TrafficSample.proxy_host_id == ph_id)).all()
    assert len(rows) == 1
    assert rows[0].requests_total == 105
    assert rows[0].by_code == {"200": 100, "404": 5}
    assert rows[0].duration_sum_s == pytest.approx(1.5)
    assert rows[0].duration_count == 105


def test_collector_skips_router_with_no_matching_proxy_host(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    # No ProxyHost at all — the router's hostname has nothing to attach to.
    _patch_traefik_transport(
        monkeypatch,
        _traefik_responder(
            routers=[
                {"name": "r1@docker", "rule": "Host(`unknown.example.com`)", "status": "enabled"}
            ],
            metrics_text='traefik_router_requests_total{code="200",router="r1@docker"} 10\n',
        ),
    )

    import asyncio

    collector = TrafficCollector(settings=get_settings())
    inserted = asyncio.run(collector.poll_once())
    assert inserted == 0

    with Session(get_engine()) as s:
        assert s.exec(select(TrafficSample)).all() == []


def test_collector_disabled_when_traefik_not_configured() -> None:
    collector = TrafficCollector(settings=get_settings())
    collector.start()  # must not raise, and must not schedule a job
    assert collector.running is False


def test_collector_prunes_samples_older_than_retention(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    monkeypatch.setenv("TRAEFIK_METRICS_RETENTION_DAYS", "1")
    config_module.reset_settings_cache()

    with Session(get_engine()) as s:
        ph = _make_proxy_host(s, service, "a.example.com")
        old = TrafficSample(
            proxy_host_id=ph.id,
            sampled_at=datetime.now(UTC) - timedelta(days=10),
            requests_total=1,
        )
        s.add(old)
        s.commit()

    # No live routers this poll — only pruning should happen.
    _patch_traefik_transport(monkeypatch, _traefik_responder(routers=[], metrics_text=""))

    import asyncio

    collector = TrafficCollector(settings=get_settings())
    asyncio.run(collector.poll_once())

    with Session(get_engine()) as s:
        assert s.exec(select(TrafficSample)).all() == []


# ---------------------------------------------------------------------------
# Wake-on-hit — the TrafficCollector-driven stand-in for a real Traefik-side
# trigger (see the module docstring and docs/traffic_metrics.md).
# ---------------------------------------------------------------------------


def _dynamic_responder(router_hostname: str, counter: list[int]) -> Any:
    """Like ``_traefik_responder``, but reads ``counter[0]`` on every call —
    so one ``_patch_traefik_transport`` covers several polls with different
    totals. (``_patch_traefik_transport`` captures ``httpx.AsyncClient`` as
    its subclass's base at call time, so patching it again mid-test would
    subclass the ALREADY-patched client instead of the real one — hence one
    patch per test, with the total varied through this counter instead.)
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/http/routers":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "r1@docker",
                        "rule": f"Host(`{router_hostname}`)",
                        "status": "enabled",
                    }
                ],
            )
        if request.url.path == "/api/http/services":
            return httpx.Response(200, json=[])
        if request.url.path == "/metrics":
            return httpx.Response(
                200,
                text=(
                    f'traefik_router_requests_total{{code="200",router="r1@docker"}} '
                    f"{counter[0]}\n"
                ),
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(404, json={})

    return handle


def _wake_runs(slug: str = "thing") -> list[AuditEntry]:
    with Session(get_engine()) as s:
        return s.exec(
            select(AuditEntry)
            .where(AuditEntry.target == f"service:{slug}", AuditEntry.action == "wake.start")
            .order_by(AuditEntry.id)
        ).all()


def _collector(monkeypatch: pytest.MonkeyPatch) -> TrafficCollector:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()
    settings = get_settings()
    return TrafficCollector(settings=settings, adapters=build_adapters(settings))


@pytest.mark.parametrize(
    ("host_wake", "service_wake", "node_wake", "expect_fire"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
        (False, False, True, False),
        (False, True, False, False),
        (True, False, False, False),
        (False, False, False, False),
    ],
)
def test_wake_on_hit_three_way_gate(
    client: TestClient,
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    host_wake: bool,
    service_wake: bool,
    node_wake: bool,
    expect_fire: bool,
) -> None:
    """For a node that can be off (and is): a wake-on-hit fires only when
    ProxyHost.wake_on_hit AND Service.wake_on_hit_enabled AND
    Node.wake_on_hit_enabled are all True — every other combination of the
    three must never fire one."""
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.id == service.id)).one()
        node = s.get(Node, svc.node_id)
        svc.wake_on_hit_enabled = service_wake
        node.wake_on_hit_enabled = node_wake
        node.always_on = False
        node.power_mgr_target = node.name
        s.add(svc)
        s.add(node)
        # FAILED — unambiguously "down" for wake-on-hit purposes.
        s.add(ServiceState(service_id=svc.id, last_verdict=HealthState.FAILED))
        s.commit()
        ph = _make_proxy_host(s, svc, "a.example.com")
        ph.wake_on_hit = host_wake
        s.add(ph)
        s.commit()

    collector = _collector(monkeypatch)
    counter = [100]
    _patch_traefik_transport(monkeypatch, _dynamic_responder("a.example.com", counter))

    import asyncio

    # First poll establishes the baseline sample (nothing to compare against
    # yet, so nothing should fire regardless of the gate).
    asyncio.run(collector.poll_once())
    assert _wake_runs() == []

    # Second poll shows a real increase — now the gate actually matters.
    counter[0] = 150
    asyncio.run(collector.poll_once())
    runs = _wake_runs()
    assert bool(runs) is expect_fire
    if expect_fire:
        assert runs[0].actor == WAKE_ON_HIT_ACTOR


@pytest.mark.parametrize("always_on", [True, False])
def test_a_node_that_is_on_needs_no_node_switch(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch, always_on: bool
) -> None:
    """The node's switch is about turning the node on. On an always-on node, or one that is on
    right now, a hit only starts the guest, so the service's own switch is enough."""
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.id == service.id)).one()
        node = s.get(Node, svc.node_id)
        node.wake_on_hit_enabled = False
        node.always_on = always_on
        node.power_mgr_target = node.name
        s.add(node)
        if not always_on:
            s.add(NodeState(node_id=node.id, power_state=PowerState.ON))
        s.add(ServiceState(service_id=svc.id, last_verdict=HealthState.DOWN))
        s.commit()
        ph = _make_proxy_host(s, svc, "a.example.com")
        ph.wake_on_hit = True
        s.add(ph)
        s.commit()

    collector = _collector(monkeypatch)
    counter = [100]
    _patch_traefik_transport(monkeypatch, _dynamic_responder("a.example.com", counter))
    import asyncio

    asyncio.run(collector.poll_once())
    counter[0] = 150
    asyncio.run(collector.poll_once())
    assert len(_wake_runs()) == 1


def test_wake_on_hit_skips_when_traffic_has_not_increased(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(get_engine()) as s:
        s.add(ServiceState(service_id=service.id, last_verdict=HealthState.FAILED))
        ph = _make_proxy_host(s, service, "a.example.com")
        ph.wake_on_hit = True
        s.add(ph)
        s.commit()

    collector = _collector(monkeypatch)
    counter = [100]
    _patch_traefik_transport(monkeypatch, _dynamic_responder("a.example.com", counter))
    import asyncio

    asyncio.run(collector.poll_once())
    assert _wake_runs() == []

    # Same counter again — Traefik saw no new requests for this host.
    asyncio.run(collector.poll_once())
    assert _wake_runs() == []


def test_wake_on_hit_skips_when_service_already_healthy(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(get_engine()) as s:
        s.add(ServiceState(service_id=service.id, last_verdict=HealthState.HEALTHY))
        ph = _make_proxy_host(s, service, "a.example.com")
        ph.wake_on_hit = True
        s.add(ph)
        s.commit()

    collector = _collector(monkeypatch)
    counter = [100]
    _patch_traefik_transport(monkeypatch, _dynamic_responder("a.example.com", counter))
    import asyncio

    asyncio.run(collector.poll_once())
    counter[0] = 150
    asyncio.run(collector.poll_once())
    assert _wake_runs() == []


def test_wake_on_hit_treats_never_scanned_as_down(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ServiceState row at all (never probed) is treated as down — there
    is no evidence the service is up, and get_or_start's cooldown stops this
    from being retried on every subsequent hit."""
    with Session(get_engine()) as s:
        ph = _make_proxy_host(s, service, "a.example.com")
        ph.wake_on_hit = True
        s.add(ph)
        s.commit()

    collector = _collector(monkeypatch)
    counter = [100]
    _patch_traefik_transport(monkeypatch, _dynamic_responder("a.example.com", counter))
    import asyncio

    asyncio.run(collector.poll_once())
    counter[0] = 150
    asyncio.run(collector.poll_once())
    assert len(_wake_runs()) == 1


def test_wake_on_hit_does_not_spam_repeated_hits(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated hits during an outage must not spam repeated wake attempts —
    the same cooldown/dedup WakePipeline.get_or_start already provides for
    the manual/API path."""
    with Session(get_engine()) as s:
        s.add(ServiceState(service_id=service.id, last_verdict=HealthState.FAILED))
        ph = _make_proxy_host(s, service, "a.example.com")
        ph.wake_on_hit = True
        s.add(ph)
        s.commit()

    collector = _collector(monkeypatch)
    counter = [100]
    _patch_traefik_transport(monkeypatch, _dynamic_responder("a.example.com", counter))
    import asyncio

    for total in (100, 150, 200, 250):
        counter[0] = total
        asyncio.run(collector.poll_once())

    # Only the first increase (100 -> 150) should have started a run; every
    # later hit reuses it via the RUNNING check in get_or_start.
    assert len(_wake_runs()) == 1


def test_wake_on_hit_inert_without_adapters(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collector built without an AdapterBundle (every pre-existing
    caller) still collects samples — the wake-on-hit half is simply off."""
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()
    with Session(get_engine()) as s:
        s.add(ServiceState(service_id=service.id, last_verdict=HealthState.FAILED))
        ph = _make_proxy_host(s, service, "a.example.com")
        ph.wake_on_hit = True
        s.add(ph)
        s.commit()

    collector = TrafficCollector(settings=get_settings())
    counter = [100]
    _patch_traefik_transport(monkeypatch, _dynamic_responder("a.example.com", counter))
    import asyncio

    asyncio.run(collector.poll_once())
    counter[0] = 150
    inserted = asyncio.run(collector.poll_once())
    assert inserted == 1
    assert _wake_runs() == []


# ---------------------------------------------------------------------------
# RouterMetricSample re-export sanity (used by the collector)
# ---------------------------------------------------------------------------


def test_router_metric_sample_is_a_plain_dataclass() -> None:
    sample = RouterMetricSample(
        router="r@docker", requests_total=1, duration_sum_s=0.1, duration_count=1, by_code={}
    )
    assert sample.router == "r@docker"


def test_wake_on_hit_never_fires_for_a_start_button_service(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traffic alone must not wake a service whose wake_trigger is "button"."""
    from orchestrator.domain.enums import WakeTrigger

    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.id == service.id)).one()
        svc.wake_trigger = WakeTrigger.BUTTON
        s.add(svc)
        s.add(ServiceState(service_id=svc.id, last_verdict=HealthState.FAILED))
        s.commit()
        ph = _make_proxy_host(s, svc, "a.example.com")
        ph.wake_on_hit = True
        s.add(ph)
        s.commit()

    collector = _collector(monkeypatch)
    counter = [100]
    _patch_traefik_transport(monkeypatch, _dynamic_responder("a.example.com", counter))

    import asyncio

    asyncio.run(collector.poll_once())
    counter[0] = 150
    asyncio.run(collector.poll_once())
    assert _wake_runs() == []
