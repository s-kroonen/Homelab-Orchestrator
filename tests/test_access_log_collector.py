"""``AccessLogCollector``: gating, host attribution WITHOUT ``TRAEFIK_API_URL``
(request host, router names via the proxy adapter, @provider stripping),
per-minute rollups (incl. the upsert across two polls), GeoIP samples, the
dedup cursor, the persisted status (incl. Traefik Manager's ``error``),
pruning, and the read endpoints/pages built on top.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import webauthn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.adapters.proxy.base import AccessLogTail, ProxyAdapter, RouteInfo, SyncEvent
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.db import session as session_module
from orchestrator.db.models import GeoTrafficSample, ProxyHost, Service, Setting, TrafficRollup
from orchestrator.db.session import get_engine
from orchestrator.registry.traefik_status import RouterInfo
from orchestrator.scheduler import access_log_collector as alc_module
from orchestrator.scheduler.access_log_collector import (
    CURSOR_SETTING_KEY,
    STATUS_SETTING_KEY,
    AccessLogCollector,
)
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app

# Log timestamps an hour ago (so retention pruning never eats them), at a
# fixed hour so minute buckets in assertions are predictable.
_BASE = (datetime.now(UTC) - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
_D = _BASE.strftime("%d/%b/%Y:%H")
_J = _BASE.strftime("%Y-%m-%dT%H")

_TM_NOT_FOUND = (
    "Access log not found. Set ACCESS_LOG_PATH env var or configure the path in Settings."
)


def _line(
    ip: str,
    ts: str,
    router: str = "my-router@docker",
    status: int = 200,
    duration: str = "1ms",
) -> str:
    return (
        f'{ip} - - [{ts}] "GET /app HTTP/1.1" {status} 1337 '
        f'"-" "-" 1 "{router}" "http://10.0.0.2:80" {duration}'
    )


def _json_line(ip: str, ts: str, **extra: Any) -> str:
    obj: dict[str, Any] = {
        "ClientHost": ip,
        "StartUTC": ts,
        "RouterName": "unknown@docker",
        "RequestPath": "/",
        "DownstreamStatus": 200,
    }
    obj.update(extra)
    return json.dumps(obj)


class _FakeProxy(ProxyAdapter):
    """Hands back whatever the test set; records what was asked."""

    def __init__(
        self,
        lines: list[str] | None,
        *,
        error: str | None = None,
        runtime: list[RouteInfo] | None = None,
        routes: list[RouteInfo] | None = None,
    ) -> None:
        self.lines = lines
        self.error = error
        self.runtime = runtime
        self.routes = routes
        self.runtime_calls = 0

    async def list_routes(self) -> list[RouteInfo] | None:
        return self.routes

    async def list_runtime_routers(self) -> list[RouteInfo] | None:
        self.runtime_calls += 1
        return self.runtime

    async def ensure_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        return []

    async def remove_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        return []

    async def create_route(self, *, hostname: str, upstream: str) -> str:
        raise NotImplementedError

    async def update_route(self, route_ref: str, *, hostname: str, upstream: str) -> None:
        raise NotImplementedError

    async def delete_route(self, route_ref: str) -> None:
        raise NotImplementedError

    async def tail_access_log(self, lines: int = 200) -> list[str] | None:
        return self.lines

    async def fetch_access_log(self, lines: int = 200) -> AccessLogTail:
        return AccessLogTail(lines=self.lines, error=self.error)


_RUNTIME = [RouteInfo(name="my-router@docker", hostnames={"app.example.com"}, upstream=None)]


def _bundle(proxy: ProxyAdapter) -> AdapterBundle:
    return AdapterBundle(
        power=DryRunPowerAdapter(),
        proxmox=DryRunProxmoxAdapter(),
        pbs=DryRunPbsAdapter(),
        proxy=proxy,
        notifier=NullNotifier(),
    )


def _proxy_host(
    session: Session, hostname: str = "app.example.com", slug: str = "svc"
) -> ProxyHost:
    svc = Service(slug=slug, name=slug.title())
    session.add(svc)
    session.commit()
    session.refresh(svc)
    ph = ProxyHost(hostname=hostname, service_id=svc.id)
    session.add(ph)
    session.commit()
    session.refresh(ph)
    return ph


def _collector(
    lines: list[str] | None, *, geoip: bool = True, **proxy_kwargs: Any
) -> tuple[AccessLogCollector, _FakeProxy]:
    if geoip:
        import os

        os.environ["GEOIP_ENABLED"] = "true"
        # A path that does not exist -> GeoIP "unavailable", never a download;
        # private IPs still bucket as "local".
        os.environ["GEOIP_DB_PATH"] = "/nonexistent/test.mmdb"
    config_module.reset_settings_cache()
    proxy_kwargs.setdefault("runtime", _RUNTIME)
    proxy = _FakeProxy(lines, **proxy_kwargs)
    return AccessLogCollector(settings=config_module.get_settings(), adapters=_bundle(proxy)), proxy


@pytest.fixture(autouse=True)
def _clean_geo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # _collector() writes os.environ directly; make monkeypatch restore it.
    monkeypatch.setenv("GEOIP_ENABLED", "false")
    monkeypatch.setenv("GEOIP_DB_PATH", "")
    monkeypatch.delenv("TRAEFIK_API_URL", raising=False)


def _rollups() -> list[TrafficRollup]:
    with Session(get_engine()) as s:
        return list(s.exec(select(TrafficRollup)).all())


def _status() -> dict[str, Any]:
    with Session(get_engine()) as s:
        row = s.get(Setting, STATUS_SETTING_KEY)
        assert row is not None
        return dict(row.value)


# ---------------------------------------------------------------------------
# gating
# ---------------------------------------------------------------------------


def test_collector_is_noop_when_access_log_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCESS_LOG_ENABLED", "false")
    config_module.reset_settings_cache()
    collector = AccessLogCollector(
        settings=config_module.get_settings(), adapters=_bundle(_FakeProxy(None))
    )
    collector.start()
    assert collector.running is False


def test_collector_is_noop_with_the_dry_run_proxy() -> None:
    collector = AccessLogCollector(
        settings=config_module.get_settings(), adapters=_bundle(DryRunProxyAdapter())
    )
    collector.start()
    assert collector.running is False


async def test_collector_starts_without_geoip_or_traefik_api_url() -> None:
    settings = config_module.get_settings()
    assert settings.geoip_enabled is False
    assert settings.traefik_api_url == ""
    collector = AccessLogCollector(settings=settings, adapters=_bundle(_FakeProxy(None)))
    try:
        collector.start()
        assert collector.running is True
    finally:
        await collector.shutdown()


def test_deprecated_geoip_poll_settings_still_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOIP_TRAFFIC_POLL_INTERVAL_S", "45")
    monkeypatch.setenv("GEOIP_ACCESS_LOG_LINES", "300")
    config_module.reset_settings_cache()
    s = config_module.get_settings()
    assert s.access_log_poll_interval_s == 45
    assert s.access_log_lines == 300
    monkeypatch.setenv("ACCESS_LOG_LINES", "700")
    config_module.reset_settings_cache()
    assert config_module.get_settings().access_log_lines == 700


# ---------------------------------------------------------------------------
# attribution (no TRAEFIK_API_URL anywhere below unless stated)
# ---------------------------------------------------------------------------


async def test_router_attribution_via_traefik_manager_runtime_routers(session: Session) -> None:
    ph = _proxy_host(session)
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")], geoip=False)
    status = await collector.poll_once()
    assert status["ok"] is True
    assert status["matched"] == 1
    rows = _rollups()
    assert [(r.proxy_host_id, r.hits) for r in rows] == [(ph.id, 1)]


async def test_router_names_match_with_provider_suffix_stripped(session: Session) -> None:
    """Traefik Manager's /api/routes names file routes bare ("my-router");
    the access log writes "my-router@file"."""
    _proxy_host(session)
    collector, _ = _collector(
        [_line("10.0.0.9", f"{_D}:42:00 +0000", router="my-router@file")],
        geoip=False,
        runtime=None,
        routes=[RouteInfo(name="my-router", hostnames={"app.example.com"}, upstream=None)],
    )
    status = await collector.poll_once()
    assert status["matched"] == 1
    assert len(_rollups()) == 1


async def test_request_host_attribution_needs_no_router_lookup(session: Session) -> None:
    ph = _proxy_host(session)
    line = _json_line(
        "203.0.113.5",
        f"{_J}:42:00Z",
        RequestHost="APP.example.com:443",
        RouterName="something-tm-never-heard-of@docker",
    )
    collector, proxy = _collector([line], geoip=False, runtime=[])
    status = await collector.poll_once()
    assert status["matched"] == 1
    assert proxy.runtime_calls == 0  # direct host hit, router map never built
    assert _rollups()[0].proxy_host_id == ph.id


async def test_router_map_is_fetched_once_per_poll(session: Session) -> None:
    _proxy_host(session)
    lines = [
        _line("10.0.0.9", f"{_D}:42:00 +0000"),
        _line("10.0.0.8", f"{_D}:42:01 +0000", router="other@docker"),
        _line("10.0.0.7", f"{_D}:42:02 +0000", router="third@docker"),
    ]
    collector, proxy = _collector(lines, geoip=False)
    await collector.poll_once()
    assert proxy.runtime_calls == 1


async def test_falls_back_to_traefik_api_when_configured(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy_host(session)
    calls: list[str] = []

    async def _fake_fetch_router_details(settings: Any) -> list[RouterInfo]:
        calls.append(settings.traefik_api_url)
        return [RouterInfo(name="legacy@docker", hostnames={"app.example.com"}, upstream=None)]

    monkeypatch.setattr(alc_module, "fetch_router_details", _fake_fetch_router_details)
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    collector, _ = _collector(
        [_line("10.0.0.9", f"{_D}:42:00 +0000", router="legacy@docker")],
        geoip=False,
        runtime=[],
    )
    status = await collector.poll_once()
    assert calls == ["http://traefik.test:8080"]
    assert status["matched"] == 1


async def test_traefik_api_is_not_consulted_when_unset(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy_host(session)

    async def _boom(settings: Any) -> None:
        raise AssertionError("must not be called without TRAEFIK_API_URL")

    monkeypatch.setattr(alc_module, "fetch_router_details", _boom)
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")], geoip=False)
    status = await collector.poll_once()
    assert status["ok"] is True and status["matched"] == 1


# ---------------------------------------------------------------------------
# rollups
# ---------------------------------------------------------------------------


async def test_rollup_counts_status_classes_and_durations(session: Session) -> None:
    _proxy_host(session)
    lines = [
        _line("10.0.0.9", f"{_D}:42:00 +0000", status=200, duration="10ms"),
        _line("10.0.0.9", f"{_D}:42:10 +0000", status=301, duration="30ms"),
        _line("10.0.0.9", f"{_D}:42:20 +0000", status=404, duration="1.5s"),
        _line("10.0.0.9", f"{_D}:42:30 +0000", status=502, duration="-"),
        _line("10.0.0.9", f"{_D}:43:05 +0000", status=200, duration="5ms"),
    ]
    collector, _ = _collector(lines, geoip=False)
    status = await collector.poll_once()
    assert status["rollups_written"] == 2
    rows = sorted(_rollups(), key=lambda r: r.bucket_start)
    first, second = rows
    assert first.bucket_start == _BASE.replace(minute=42)
    assert (first.hits, first.s2xx, first.s3xx, first.s4xx, first.s5xx) == (4, 1, 1, 1, 1)
    assert first.duration_ms_count == 3
    assert first.duration_ms_sum == pytest.approx(1540.0)
    assert first.duration_ms_max == pytest.approx(1500.0)
    assert second.bucket_start == _BASE.replace(minute=43)
    assert second.hits == 1


async def test_rollup_upserts_when_a_minute_spans_two_polls(session: Session) -> None:
    _proxy_host(session)
    tail = [_line("10.0.0.9", f"{_D}:42:05 +0000", duration="20ms")]
    collector, proxy = _collector(tail, geoip=False)
    await collector.poll_once()

    proxy.lines = [
        *tail,
        _line("10.0.0.9", f"{_D}:42:40 +0000", status=500, duration="80ms"),
    ]
    status = await collector.poll_once()
    assert status["new_entries"] == 1

    rows = _rollups()
    assert len(rows) == 1  # same minute -> same row, added into
    row = rows[0]
    assert row.hits == 2
    assert row.s2xx == 1 and row.s5xx == 1
    assert row.duration_ms_count == 2
    assert row.duration_ms_sum == pytest.approx(100.0)
    assert row.duration_ms_max == pytest.approx(80.0)


async def test_unmatched_lines_write_no_rollups(session: Session) -> None:
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")], geoip=False)
    status = await collector.poll_once()
    assert status["matched"] == 0
    assert status["rollups_written"] == 0
    assert _rollups() == []


# ---------------------------------------------------------------------------
# status (persisted + warnings)
# ---------------------------------------------------------------------------


class _RecordingLog:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))

    def info(self, *a: Any, **kw: Any) -> None:
        pass

    def error(self, *a: Any, **kw: Any) -> None:
        pass


async def test_status_persists_traefik_manager_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _RecordingLog()
    monkeypatch.setattr(alc_module, "log", rec)
    collector, _ = _collector(None, geoip=False, error=f"Traefik Manager: {_TM_NOT_FOUND}")
    await collector.poll_once()
    status = _status()
    assert status["ok"] is False
    assert _TM_NOT_FOUND in status["error"]
    assert status["lines_read"] == 0
    assert status["last_poll_at"]
    assert any(e == "access_log_collector.tail_error" for e, _ in rec.warnings)


async def test_status_counts_and_unmatched_routers(session: Session) -> None:
    _proxy_host(session)
    lines = [
        _line("10.0.0.9", f"{_D}:42:00 +0000"),
        _line("10.0.0.9", f"{_D}:42:01 +0000", router="ghost@docker"),
        _line("10.0.0.9", f"{_D}:42:02 +0000", router="ghost@docker"),
        _line("10.0.0.9", f"{_D}:42:03 +0000", router="other@file"),
        "garbage line",
        "",
    ]
    collector, _ = _collector(lines, geoip=False)
    await collector.poll_once()
    status = _status()
    assert status["ok"] is True and status["error"] is None
    assert status["lines_read"] == 6
    assert status["parsed"] == 4
    assert status["unparsable"] == 1
    assert status["new_entries"] == 4
    assert status["matched"] == 1
    assert status["unmatched_routers"] == [
        {"router": "ghost@docker", "hits": 2},
        {"router": "other@file", "hits": 1},
    ]
    assert status["rollups_written"] == 1
    assert status["geo_samples_written"] == 0


async def test_warns_when_lines_parse_but_nothing_matches(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _RecordingLog()
    monkeypatch.setattr(alc_module, "log", rec)
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")], geoip=False)
    await collector.poll_once()
    events = dict(rec.warnings)
    assert "access_log_collector.nothing_matched" in events
    assert events["access_log_collector.nothing_matched"]["unmatched_routers"] == [
        "my-router@docker"
    ]


async def test_warns_when_nothing_parses(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _RecordingLog()
    monkeypatch.setattr(alc_module, "log", rec)
    collector, _ = _collector(["not a log line", "neither is this"], geoip=False)
    status = await collector.poll_once()
    assert status["parsed"] == 0 and status["unparsable"] == 2
    assert any(e == "access_log_collector.unparsable" for e, _ in rec.warnings)


# ---------------------------------------------------------------------------
# GeoIP samples (GEOIP_ENABLED only)
# ---------------------------------------------------------------------------


async def test_geo_samples_one_per_distinct_ip(session: Session) -> None:
    _proxy_host(session)
    lines = [
        _line("10.0.0.9", f"{_D}:42:00 +0000"),
        _line("10.0.0.9", f"{_D}:42:01 +0000"),
        _line("10.0.0.8", f"{_D}:42:02 +0000"),
    ]
    collector, _ = _collector(lines)
    status = await collector.poll_once()
    assert status["geo_samples_written"] == 2

    with Session(get_engine()) as s:
        rows = s.exec(select(GeoTrafficSample)).all()
    by_ip = {r.client_ip: r for r in rows}
    assert by_ip["10.0.0.9"].hit_count == 2
    assert by_ip["10.0.0.8"].hit_count == 1
    assert by_ip["10.0.0.9"].bucket == "local"


async def test_no_geo_samples_when_geoip_disabled(session: Session) -> None:
    _proxy_host(session)
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")], geoip=False)
    status = await collector.poll_once()
    assert status["rollups_written"] == 1
    with Session(get_engine()) as s:
        assert s.exec(select(GeoTrafficSample)).all() == []


# ---------------------------------------------------------------------------
# dedup cursor: re-polling an overlapping tail must not double-count
# ---------------------------------------------------------------------------


async def test_repolling_the_same_tail_does_not_double_count(session: Session) -> None:
    _proxy_host(session)
    tail = [
        _line("10.0.0.9", f"{_D}:42:00 +0000"),
        _line("10.0.0.9", f"{_D}:42:01 +0000"),
    ]
    collector, _ = _collector(tail)
    first = await collector.poll_once()
    assert first["geo_samples_written"] == 1
    second = await collector.poll_once()
    assert second["new_entries"] == 0
    assert second["geo_samples_written"] == 0
    assert second["matched"] == 2  # status still describes the whole tail

    with Session(get_engine()) as s:
        rows = s.exec(select(GeoTrafficSample)).all()
    assert len(rows) == 1 and rows[0].hit_count == 2
    assert [r.hits for r in _rollups()] == [2]


async def test_repolling_an_overlapping_but_grown_tail_only_counts_new_lines(
    session: Session,
) -> None:
    _proxy_host(session)
    tail = [
        _line("10.0.0.9", f"{_D}:42:00 +0000"),
        _line("10.0.0.9", f"{_D}:42:01 +0000"),
    ]
    collector, proxy = _collector(tail)
    await collector.poll_once()
    proxy.lines = [*tail, _line("10.0.0.8", f"{_D}:42:05 +0000")]
    second = await collector.poll_once()
    assert second["new_entries"] == 1
    assert second["geo_samples_written"] == 1

    with Session(get_engine()) as s:
        rows = {r.client_ip: r for r in s.exec(select(GeoTrafficSample)).all()}
    assert rows["10.0.0.9"].hit_count == 2
    assert rows["10.0.0.8"].hit_count == 1
    assert [r.hits for r in _rollups()] == [3]


async def test_cursor_is_persisted_in_the_setting_table(session: Session) -> None:
    _proxy_host(session)
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")])
    await collector.poll_once()
    with Session(get_engine()) as s:
        row = s.get(Setting, CURSOR_SETTING_KEY)
    assert row is not None
    assert row.value["last_ts"] is not None
    assert row.value["last_hashes"]


# ---------------------------------------------------------------------------
# pruning
# ---------------------------------------------------------------------------


async def test_prune_removes_old_geo_samples_and_rollups(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEOIP_TRAFFIC_RETENTION_DAYS", "1")
    monkeypatch.setenv("TRAFFIC_ROLLUP_RETENTION_DAYS", "1")
    ph = _proxy_host(session)
    old = datetime.now(UTC) - timedelta(days=10)
    session.add(
        GeoTrafficSample(proxy_host_id=ph.id, sampled_at=old, client_ip="1.2.3.4", hit_count=1)
    )
    session.add(
        TrafficRollup(proxy_host_id=ph.id, bucket_start=old.replace(second=0, microsecond=0))
    )
    fresh = datetime.now(UTC).replace(second=0, microsecond=0)
    session.add(TrafficRollup(proxy_host_id=ph.id, bucket_start=fresh, hits=1))
    session.commit()

    collector, _ = _collector([])
    await collector.poll_once()

    with Session(get_engine()) as s:
        assert s.exec(select(GeoTrafficSample)).all() == []
    assert [r.bucket_start for r in _rollups()] == [fresh]


# ---------------------------------------------------------------------------
# HTTP: /api/traffic/geo, /api/traffic/series, /api/traffic/collector-status
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


def test_geo_endpoint_returns_disabled_shape_by_default(client: TestClient) -> None:
    r = client.get("/api/traffic/geo")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "enabled": False,
        "range": "24h",
        "countries": [],
        "top_ips": [],
        "sample_count": 0,
    }


def _seed_one_host_samples() -> int:
    with Session(get_engine()) as s:
        svc = Service(slug="svc", name="svc")
        s.add(svc)
        s.commit()
        s.refresh(svc)
        ph = ProxyHost(hostname="app.example.com", service_id=svc.id)
        s.add(ph)
        s.commit()
        s.refresh(ph)
        assert ph.id is not None
        s.add(
            GeoTrafficSample(
                proxy_host_id=ph.id,
                client_ip="8.8.8.8",
                bucket="country",
                country_code="US",
                country_name="United States",
                hit_count=3,
            )
        )
        s.add(
            GeoTrafficSample(proxy_host_id=ph.id, client_ip="10.0.0.9", bucket="local", hit_count=2)
        )
        s.commit()
        return ph.id


def test_geo_endpoint_aggregates_when_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEOIP_ENABLED", "true")
    config_module.reset_settings_cache()
    _seed_one_host_samples()

    body = client.get("/api/traffic/geo").json()
    assert body["enabled"] is True
    assert body["sample_count"] == 2
    codes = {c["country_code"] or c["bucket"]: c["hits"] for c in body["countries"]}
    assert codes == {"US": 3, "local": 2}
    ips = {i["client_ip"]: i["hits"] for i in body["top_ips"]}
    assert ips == {"8.8.8.8": 3, "10.0.0.9": 2}


def test_geo_endpoint_range_filters_on_sampled_at(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEOIP_ENABLED", "true")
    config_module.reset_settings_cache()
    ph_id = _seed_one_host_samples()
    with Session(get_engine()) as s:
        s.add(
            GeoTrafficSample(
                proxy_host_id=ph_id,
                sampled_at=datetime.now(UTC) - timedelta(hours=3),
                client_ip="145.1.2.3",
                bucket="country",
                country_code="NL",
                country_name="Netherlands",
                hit_count=4,
            )
        )
        s.commit()

    one_hour = client.get("/api/traffic/geo?range=1h").json()
    assert one_hour["range"] == "1h"
    assert {c["country_code"] or c["bucket"] for c in one_hour["countries"]} == {"US", "local"}
    six_hours = client.get("/api/traffic/geo?range=6h").json()
    assert {c["country_code"] or c["bucket"] for c in six_hours["countries"]} == {
        "US",
        "local",
        "NL",
    }
    assert client.get("/api/traffic/geo?range=3y").status_code == 422


def test_geo_endpoint_requires_auth() -> None:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        assert c.get("/api/traffic/geo").status_code in (401, 403)
        assert c.get("/api/traffic/series").status_code in (401, 403)
        assert c.get("/api/traffic/collector-status").status_code in (401, 403)


def _seed_rollups() -> tuple[int, int]:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    with Session(get_engine()) as s:
        a = Service(slug="alpha", name="Alpha")
        b = Service(slug="beta", name="Beta")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        pa = ProxyHost(hostname="alpha.example.com", service_id=a.id)
        pb = ProxyHost(hostname="beta.example.com", service_id=b.id)
        s.add(pa)
        s.add(pb)
        s.commit()
        s.refresh(pa)
        s.refresh(pb)
        assert pa.id is not None and pb.id is not None
        s.add(
            TrafficRollup(
                proxy_host_id=pa.id,
                bucket_start=now - timedelta(minutes=5),
                hits=10,
                s2xx=8,
                s4xx=1,
                s5xx=1,
                duration_ms_sum=200.0,
                duration_ms_count=10,
                duration_ms_max=90.0,
            )
        )
        s.add(
            TrafficRollup(
                proxy_host_id=pb.id,
                bucket_start=now - timedelta(minutes=5),
                hits=4,
                s2xx=4,
                duration_ms_sum=40.0,
                duration_ms_count=4,
                duration_ms_max=15.0,
            )
        )
        # Outside a 1h window, inside 24h.
        s.add(
            TrafficRollup(
                proxy_host_id=pa.id, bucket_start=now - timedelta(hours=3), hits=7, s2xx=7
            )
        )
        s.commit()
        return pa.id, pb.id


@pytest.mark.parametrize(
    ("range_key", "step", "count"),
    [
        ("15m", 60, 15),
        ("1h", 60, 60),
        ("6h", 300, 72),
        ("24h", 900, 96),
        ("7d", 3600, 168),
        ("14d", 3600, 336),
    ],
)
def test_series_step_selection_and_zero_fill(
    client: TestClient, range_key: str, step: int, count: int
) -> None:
    body = client.get(f"/api/traffic/series?range={range_key}").json()
    assert body["range"] == range_key
    assert body["step_s"] == step
    assert len(body["buckets"]) == count
    for key in ("hits", "s2xx", "s3xx", "s4xx", "s5xx", "avg_ms", "max_ms"):
        assert len(body["overall"][key]) == count
    assert body["overall"]["hits"] == [0] * count
    assert body["overall"]["avg_ms"] == [None] * count
    first = datetime.fromisoformat(body["buckets"][0].replace("Z", "+00:00"))
    second = datetime.fromisoformat(body["buckets"][1].replace("Z", "+00:00"))
    assert (second - first).total_seconds() == step
    assert int(first.timestamp()) % step == 0


def test_series_aggregates_rollups(client: TestClient) -> None:
    _seed_rollups()
    body = client.get("/api/traffic/series?range=1h").json()
    o = body["overall"]
    assert sum(o["hits"]) == 14
    assert sum(o["s5xx"]) == 1
    i = next(k for k, v in enumerate(o["hits"]) if v)
    assert o["avg_ms"][i] == pytest.approx(240.0 / 14, abs=0.1)
    assert o["max_ms"][i] == 90.0
    assert body["totals"]["hits"] == 14
    hosts = {h["hostname"]: h for h in body["hosts"]}
    assert hosts["alpha.example.com"]["totals"] == {
        "hits": 10,
        "s4xx": 1,
        "s5xx": 1,
        "avg_ms": 20.0,
        "max_ms": 90.0,
    }
    assert hosts["alpha.example.com"]["service_slug"] == "alpha"
    assert body["hosts"][0]["hostname"] == "alpha.example.com"  # sorted by hits

    day = client.get("/api/traffic/series?range=24h").json()
    assert sum(day["overall"]["hits"]) == 21


def test_series_slug_scopes_overall_but_lists_every_host(client: TestClient) -> None:
    _seed_rollups()
    body = client.get("/api/traffic/series?range=1h&slug=beta").json()
    assert sum(body["overall"]["hits"]) == 4
    assert body["slug"] == "beta"
    assert {h["hostname"] for h in body["hosts"]} == {"alpha.example.com", "beta.example.com"}


def test_series_rejects_unknown_range(client: TestClient) -> None:
    assert client.get("/api/traffic/series?range=2h").status_code == 422


def test_collector_status_endpoint(client: TestClient) -> None:
    body = client.get("/api/traffic/collector-status").json()
    assert body["status"] is None
    assert body["view"]["level"] == "off"

    with Session(get_engine()) as s:
        s.add(
            Setting(
                key=STATUS_SETTING_KEY,
                value={
                    "last_poll_at": datetime.now(UTC).isoformat(),
                    "ok": False,
                    "error": f"Traefik Manager: {_TM_NOT_FOUND}",
                    "lines_read": 0,
                    "parsed": 0,
                    "unparsable": 0,
                    "new_entries": 0,
                    "matched": 0,
                    "unmatched_routers": [],
                    "rollups_written": 0,
                    "geo_samples_written": 0,
                },
            )
        )
        s.commit()
    body = client.get("/api/traffic/collector-status").json()
    assert body["status"]["ok"] is False
    assert body["view"]["level"] == "error"
    assert _TM_NOT_FOUND in body["view"]["error"]


# ---------------------------------------------------------------------------
# pages: status strip / notice
# ---------------------------------------------------------------------------


def _store_status(**overrides: Any) -> None:
    value: dict[str, Any] = {
        "last_poll_at": datetime.now(UTC).isoformat(),
        "ok": True,
        "error": None,
        "lines_read": 184,
        "parsed": 184,
        "unparsable": 0,
        "new_entries": 12,
        "matched": 180,
        "unmatched_routers": [{"router": "ghost@docker", "hits": 4}],
        "rollups_written": 3,
        "geo_samples_written": 0,
    }
    value.update(overrides)
    with Session(get_engine()) as s:
        s.add(Setting(key=STATUS_SETTING_KEY, value=value))
        s.commit()


def test_traffic_page_requires_login(app: Any) -> None:
    with TestClient(app) as anon:
        r = anon.get("/traffic", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_traffic_page_not_configured_notice(client: TestClient) -> None:
    text = client.get("/traffic").text
    assert 'data-level="off"' in text
    assert "PROXY_ADAPTER=traefik_manager" in text
    assert "TRAEFIK_API_URL is not required" in text
    assert "location.reload" not in text
    assert "/static/timecontrols.js" in text
    assert "data-tc-range" in text and "data-tc-refresh" in text


def test_traffic_page_status_strip(client: TestClient) -> None:
    _store_status()
    text = client.get("/traffic?range=1h").text
    assert 'data-level="ok"' in text
    assert "184 lines" in text and "180 matched" in text
    assert "4 unmatched" in text
    assert '<option value="1h" selected>' in text


def test_traffic_page_error_notice_quotes_traefik_manager(client: TestClient) -> None:
    _store_status(ok=False, error=f"Traefik Manager: {_TM_NOT_FOUND}", lines_read=0, matched=0)
    text = client.get("/traffic").text
    assert 'class="notice notice-error"' in text
    assert "Set ACCESS_LOG_PATH env var" in text
    assert "accessLog" in text


def test_pages_list_unmatched_routers_when_nothing_matched(client: TestClient) -> None:
    _store_status(matched=0, unmatched_routers=[{"router": "ghost@docker", "hits": 184}])
    for path in ("/traffic", "/traffic/geo"):
        text = client.get(path).text
        assert "notice-warn" in text
        assert "<code>ghost@docker</code>" in text
        assert 'href="/proxy-hosts"' in text


def test_dashboard_links_to_traffic_page(client: TestClient) -> None:
    assert 'href="/traffic"' in client.get("/").text


# ---------------------------------------------------------------------------
# /traffic/geo page
# ---------------------------------------------------------------------------


def _seed_geo_samples() -> None:
    with Session(get_engine()) as s:
        alpha = Service(slug="alpha", name="Alpha")
        beta = Service(slug="beta", name="Beta")
        s.add(alpha)
        s.add(beta)
        s.commit()
        s.refresh(alpha)
        s.refresh(beta)
        ph_a = ProxyHost(hostname="alpha.example.com", service_id=alpha.id)
        ph_b = ProxyHost(hostname="beta.example.com", service_id=beta.id)
        s.add(ph_a)
        s.add(ph_b)
        s.commit()
        s.refresh(ph_a)
        s.refresh(ph_b)
        s.add(
            GeoTrafficSample(
                proxy_host_id=ph_a.id,
                client_ip="8.8.8.8",
                bucket="country",
                country_code="US",
                country_name="United States",
                hit_count=3,
            )
        )
        s.add(
            GeoTrafficSample(
                proxy_host_id=ph_a.id, client_ip="10.0.0.9", bucket="local", hit_count=1
            )
        )
        s.add(
            GeoTrafficSample(
                proxy_host_id=ph_b.id,
                client_ip="145.1.2.3",
                bucket="country",
                country_code="NL",
                country_name="Netherlands",
                hit_count=4,
            )
        )
        s.commit()


def test_geo_page_requires_login(app: Any) -> None:
    with TestClient(app) as anon:
        r = anon.get("/traffic/geo", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_geo_page_disabled_notice(client: TestClient) -> None:
    r = client.get("/traffic/geo")
    assert r.status_code == 200
    assert "GEOIP_ENABLED" in r.text
    assert "geoip_traffic.md" in r.text
    assert "<table" not in r.text


def test_geo_page_renders_countries_and_ips(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEOIP_ENABLED", "true")
    config_module.reset_settings_cache()
    _seed_geo_samples()

    r = client.get("/traffic/geo")
    assert r.status_code == 200
    text = r.text
    assert "United States" in text
    assert "Netherlands" in text
    assert "Local network" in text
    assert "\U0001f1fa\U0001f1f8" in text  # US flag
    assert "\U0001f1f3\U0001f1f1" in text  # NL flag
    assert "<code>8.8.8.8</code>" in text
    assert "<code>10.0.0.9</code>" in text
    assert "<strong>8</strong> hits" in text
    assert "local 12.5% / external 87.5%" in text
    assert 'value="alpha"' in text and 'value="beta"' in text
    assert 'class="table-scroll"' in text
    assert "data-tc-range" in text
    assert 'id="collector-status"' in text


def test_geo_page_slug_filter(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOIP_ENABLED", "true")
    config_module.reset_settings_cache()
    _seed_geo_samples()

    r = client.get("/traffic/geo?slug=beta")
    assert r.status_code == 200
    assert "Netherlands" in r.text
    assert "United States" not in r.text
    assert "8.8.8.8" not in r.text
    assert '<option value="beta" selected>' in r.text

    r = client.get("/traffic/geo?slug=")
    assert "United States" in r.text and "Netherlands" in r.text


def test_geo_page_range_filter(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOIP_ENABLED", "true")
    config_module.reset_settings_cache()
    _seed_geo_samples()
    with Session(get_engine()) as s:
        for row in s.exec(select(GeoTrafficSample)).all():
            if row.country_code == "NL":
                row.sampled_at = datetime.now(UTC) - timedelta(days=3)
                s.add(row)
        s.commit()
    assert "Netherlands" not in client.get("/traffic/geo?range=24h").text
    text = client.get("/traffic/geo?range=7d").text
    assert "Netherlands" in text
    assert '<option value="7d" selected>' in text


def test_flag_emoji_helper() -> None:
    from orchestrator.web.routers.ui import _flag_emoji

    assert _flag_emoji("nl") == "\U0001f1f3\U0001f1f1"
    assert _flag_emoji("") == ""
    assert _flag_emoji("XYZ") == ""


def test_traffic_pages_link_to_each_other(client: TestClient) -> None:
    assert 'href="/traffic/geo"' in client.get("/traffic").text
    assert 'href="/traffic"' in client.get("/traffic/geo").text


# ---------------------------------------------------------------------------
# wake-on-hit from the access log (no TRAEFIK_API_URL / Prometheus needed)
# ---------------------------------------------------------------------------


def _wake_starts() -> list[Any]:
    from orchestrator.db.models import AuditEntry

    with Session(get_engine()) as s:
        return list(s.exec(select(AuditEntry).where(AuditEntry.action == "wake.start")).all())


def _hit_setup(session: Session, service: Service, **ph_flags: Any) -> None:
    ph = ProxyHost(hostname="app.example.com", service_id=service.id, **ph_flags)
    session.add(ph)
    session.commit()


async def test_a_hit_on_a_down_service_wakes_it(session: Session, service: Service) -> None:
    from orchestrator.db.models import ServiceState
    from orchestrator.domain.enums import HealthState
    from orchestrator.pipelines.wake_on_hit import WAKE_ON_HIT_ACTOR

    session.add(ServiceState(service_id=service.id, last_verdict=HealthState.FAILED))
    _hit_setup(session, service, wake_on_hit=True)
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")], geoip=False)

    status = await collector.poll_once()

    assert status["woken"] == ["thing"]
    (start,) = _wake_starts()
    assert start.actor == WAKE_ON_HIT_ACTOR and start.target == "service:thing"
    # the same lines again are not new traffic: nothing more happens
    again = await collector.poll_once()
    assert again["woken"] == [] and len(_wake_starts()) == 1


@pytest.mark.parametrize("why", ["host_off", "service_off", "node_off", "button", "up"])
async def test_the_gates_still_apply(session: Session, service: Service, why: str) -> None:
    from orchestrator.db.models import Node, ServiceState
    from orchestrator.domain.enums import HealthState, WakeTrigger

    svc = session.get(Service, service.id)
    assert svc is not None
    state = HealthState.HEALTHY if why == "up" else HealthState.FAILED
    session.add(ServiceState(service_id=svc.id, last_verdict=state))
    if why == "service_off":
        svc.wake_on_hit_enabled = False
    if why == "button":
        svc.wake_trigger = WakeTrigger.BUTTON
    if why == "node_off":
        node = session.get(Node, svc.node_id)
        assert node is not None
        node.wake_on_hit_enabled = False
        session.add(node)
    session.add(svc)
    _hit_setup(session, service, wake_on_hit=why != "host_off")
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")], geoip=False)

    status = await collector.poll_once()

    assert status["woken"] == [] and _wake_starts() == []
    assert status["matched"] == 1  # the rollups are written regardless


async def test_a_wake_that_cannot_start_never_breaks_the_rollups(
    session: Session, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.pipelines import wake_on_hit

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("no power adapter")

    monkeypatch.setattr(wake_on_hit.WakePipeline, "get_or_start", boom)
    _hit_setup(session, service, wake_on_hit=True)
    collector, _ = _collector([_line("10.0.0.9", f"{_D}:42:00 +0000")], geoip=False)

    status = await collector.poll_once()

    assert status["ok"] is True and status["woken"] == []
    assert len(_rollups()) == 1
