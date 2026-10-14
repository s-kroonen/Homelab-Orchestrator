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
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import ProxyHost, Service, TrafficSample
from orchestrator.db.session import get_engine
from orchestrator.registry.traefik_status import RouterMetricSample, parse_router_metrics
from orchestrator.scheduler.traffic_collector import TrafficCollector
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app
from orchestrator.web.routers.traffic import build_traffic_view

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
# build_traffic_view (rate / sparkline / code-breakdown derivation)
# ---------------------------------------------------------------------------


def test_build_traffic_view_computes_rate_from_two_samples(
    client: TestClient, service: Service
) -> None:
    with Session(get_engine()) as s:
        ph = _make_proxy_host(s, service, "a.example.com")
        t0 = datetime.now(UTC) - timedelta(seconds=60)
        t1 = datetime.now(UTC)
        s.add(
            TrafficSample(
                proxy_host_id=ph.id,
                sampled_at=t0,
                requests_total=100,
                by_code={"200": 100},
            )
        )
        s.add(
            TrafficSample(
                proxy_host_id=ph.id,
                sampled_at=t1,
                requests_total=160,
                by_code={"200": 155, "404": 5},
            )
        )
        s.commit()

        view = build_traffic_view(s, traefik_enabled=True)

    assert view["enabled"] is True
    assert view["has_samples"] is True
    # 60 requests over 60s = 60 req/min.
    assert view["overall"]["requests_per_min"] == pytest.approx(60.0)
    host_rows = [h for grp in view["groups"] for h in grp["hosts"]]
    assert len(host_rows) == 1
    row = host_rows[0]
    assert row["rate_per_min"] == pytest.approx(60.0)
    assert row["code_breakdown"] == {"2xx": 55, "3xx": 0, "4xx": 5, "5xx": 0, "other": 0}
    assert row["sparkline"] is not None


def test_build_traffic_view_treats_counter_reset_as_no_rate(
    client: TestClient, service: Service
) -> None:
    with Session(get_engine()) as s:
        ph = _make_proxy_host(s, service, "a.example.com")
        s.add(
            TrafficSample(
                proxy_host_id=ph.id,
                sampled_at=datetime.now(UTC) - timedelta(seconds=60),
                requests_total=500,
            )
        )
        # Traefik restarted: counter dropped.
        s.add(TrafficSample(proxy_host_id=ph.id, sampled_at=datetime.now(UTC), requests_total=3))
        s.commit()

        view = build_traffic_view(s, traefik_enabled=True)

    row = next(h for grp in view["groups"] for h in grp["hosts"])
    assert row["rate_per_min"] is None
    assert row["restarted"] is True


def test_build_traffic_view_disabled_short_circuits(client: TestClient) -> None:
    with Session(get_engine()) as s:
        view = build_traffic_view(s, traefik_enabled=False)
    assert view == {
        "enabled": False,
        "has_samples": False,
        "overall": None,
        "groups": [],
        "host_count": 0,
    }


# ---------------------------------------------------------------------------
# /traffic page
# ---------------------------------------------------------------------------


def test_traffic_page_requires_login(app: Any) -> None:
    with TestClient(app) as anon:
        r = anon.get("/traffic", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_traffic_page_not_configured(client: TestClient) -> None:
    r = client.get("/traffic")
    assert r.status_code == 200
    assert "not configured" in r.text.lower()


def test_traffic_page_configured_but_no_samples(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()
    r = client.get("/traffic")
    assert r.status_code == 200
    assert "no traffic samples yet" in r.text.lower()


def test_traffic_page_renders_with_samples(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()

    with Session(get_engine()) as s:
        ph = _make_proxy_host(s, service, "a.example.com")
        s.add(
            TrafficSample(
                proxy_host_id=ph.id,
                sampled_at=datetime.now(UTC) - timedelta(seconds=60),
                requests_total=10,
            )
        )
        s.add(TrafficSample(proxy_host_id=ph.id, sampled_at=datetime.now(UTC), requests_total=70))
        s.commit()

    r = client.get("/traffic")
    assert r.status_code == 200
    assert "a.example.com" in r.text
    assert service.name in r.text
    assert "requests/min" in r.text.lower()


def test_dashboard_links_to_traffic_page(client: TestClient) -> None:
    assert 'href="/traffic"' in client.get("/").text


# ---------------------------------------------------------------------------
# RouterMetricSample re-export sanity (used by the collector)
# ---------------------------------------------------------------------------


def test_router_metric_sample_is_a_plain_dataclass() -> None:
    sample = RouterMetricSample(
        router="r@docker", requests_total=1, duration_sum_s=0.1, duration_count=1, by_code={}
    )
    assert sample.router == "r@docker"
