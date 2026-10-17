"""``GeoTrafficCollector``: GEOIP_ENABLED gating, router -> ProxyHost
attribution, the dedup/cursor regression test (re-polling an overlapping
tail must not double-count), pruning, and the read endpoint.
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
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.adapters.proxy.base import ProxyAdapter, RouteInfo, SyncEvent
from orchestrator.db import session as session_module
from orchestrator.db.models import GeoTrafficSample, ProxyHost, Service, Setting
from orchestrator.db.session import get_engine
from orchestrator.registry.traefik_status import RouterInfo
from orchestrator.scheduler import geo_traffic_collector as gtc_module
from orchestrator.scheduler.geo_traffic_collector import CURSOR_SETTING_KEY, GeoTrafficCollector
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app


def _line(ip: str, ts: str, router: str = "my-router@docker", status: int = 200) -> str:
    return (
        f'{ip} - - [{ts}] "GET /app HTTP/1.1" {status} 1337 '
        f'"-" "-" 1 "{router}" "http://10.0.0.2:80" 1ms'
    )


class _FakeTailProxyAdapter(ProxyAdapter):
    """Hands back whatever ``lines`` the test set, ignoring everything else
    — the rest of ``ProxyAdapter`` is irrelevant to this collector."""

    def __init__(self, lines: list[str] | None) -> None:
        self.lines = lines

    async def list_routes(self) -> list[RouteInfo] | None:
        return None

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


def _bundle(proxy: ProxyAdapter) -> AdapterBundle:
    return AdapterBundle(
        power=DryRunPowerAdapter(),
        proxmox=DryRunProxmoxAdapter(),
        pbs=DryRunPbsAdapter(),
        proxy=proxy,
        notifier=NullNotifier(),
    )


def _proxy_host(session: Session, hostname: str = "app.example.com") -> ProxyHost:
    svc = Service(slug="svc", name="svc")
    session.add(svc)
    session.commit()
    session.refresh(svc)
    ph = ProxyHost(hostname=hostname, service_id=svc.id)
    session.add(ph)
    session.commit()
    session.refresh(ph)
    return ph


def _collector(lines: list[str] | None, monkeypatch: pytest.MonkeyPatch) -> GeoTrafficCollector:
    """A collector whose GeoIP always resolves (no real DB) and whose router
    lookup resolves ``my-router@docker`` -> the fixture's ProxyHost hostname,
    without touching Traefik's own API."""

    async def _fake_fetch_router_details(settings: Any) -> list[RouterInfo]:
        return [RouterInfo(name="my-router@docker", hostnames={"app.example.com"}, upstream=None)]

    monkeypatch.setattr(gtc_module, "fetch_router_details", _fake_fetch_router_details)

    collector = GeoTrafficCollector(
        settings=config_module.get_settings(), adapters=_bundle(_FakeTailProxyAdapter(lines))
    )
    # GeoIP disabled in settings (default) — resolve() still works fine for
    # "local" bucketing without ensure_ready(); real-country resolution is
    # covered by test_geoip.py, not re-tested here.
    return collector


# ---------------------------------------------------------------------------
# GEOIP_ENABLED gating
# ---------------------------------------------------------------------------


def test_collector_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOIP_ENABLED", "false")
    config_module.reset_settings_cache()
    collector = GeoTrafficCollector(
        settings=config_module.get_settings(), adapters=_bundle(_FakeTailProxyAdapter(None))
    )
    collector.start()
    assert collector.running is False


async def test_collector_starts_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOIP_ENABLED", "true")
    config_module.reset_settings_cache()
    collector = GeoTrafficCollector(
        settings=config_module.get_settings(), adapters=_bundle(_FakeTailProxyAdapter(None))
    )
    try:
        collector.start()
        assert collector.running is True
    finally:
        await collector.shutdown()


async def test_poll_once_returns_zero_when_tail_is_none(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy_host(session)
    collector = _collector(None, monkeypatch)
    assert await collector.poll_once() == 0


# ---------------------------------------------------------------------------
# Attribution + local bucketing
# ---------------------------------------------------------------------------


async def test_poll_once_stores_one_sample_per_distinct_ip_for_a_matched_router(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy_host(session)
    lines = [
        _line("10.0.0.9", "10/Jun/2026:08:42:00 +0000"),
        _line("10.0.0.9", "10/Jun/2026:08:42:01 +0000"),
        _line("10.0.0.8", "10/Jun/2026:08:42:02 +0000"),
    ]
    collector = _collector(lines, monkeypatch)
    inserted = await collector.poll_once()
    assert inserted == 2  # two distinct IPs

    with Session(get_engine()) as s:
        rows = s.exec(select(GeoTrafficSample)).all()
    by_ip = {r.client_ip: r for r in rows}
    assert by_ip["10.0.0.9"].hit_count == 2
    assert by_ip["10.0.0.8"].hit_count == 1
    # 10.x.x.x is private -> the "local" bucket, not "unknown".
    assert by_ip["10.0.0.9"].bucket == "local"


async def test_poll_once_skips_lines_whose_router_has_no_matching_proxy_host(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ProxyHost registered at all — the router lookup resolves to a
    # hostname nothing in the DB has.
    lines = [_line("10.0.0.9", "10/Jun/2026:08:42:00 +0000")]
    collector = _collector(lines, monkeypatch)
    inserted = await collector.poll_once()
    assert inserted == 0
    with Session(get_engine()) as s:
        assert s.exec(select(GeoTrafficSample)).all() == []


async def test_poll_once_skips_unparsable_lines_without_crashing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy_host(session)
    lines = ["total garbage, not a log line", _line("10.0.0.9", "10/Jun/2026:08:42:00 +0000")]
    collector = _collector(lines, monkeypatch)
    inserted = await collector.poll_once()
    assert inserted == 1


# ---------------------------------------------------------------------------
# THE regression test: re-polling an overlapping tail must not double-count
# ---------------------------------------------------------------------------


async def test_repolling_the_same_tail_does_not_double_count(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy_host(session)
    initial = [
        _line("10.0.0.9", "10/Jun/2026:08:42:00 +0000"),
        _line("10.0.0.9", "10/Jun/2026:08:42:01 +0000"),
    ]
    collector = _collector(initial, monkeypatch)

    first = await collector.poll_once()
    assert first == 1  # one distinct IP

    # The access log hasn't grown — Traefik Manager hands back the exact
    # same tail (the fixture's ``lines`` never changed). A second poll must
    # see nothing new.
    second = await collector.poll_once()
    assert second == 0

    with Session(get_engine()) as s:
        rows = s.exec(select(GeoTrafficSample)).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 2  # from the FIRST poll only


async def test_repolling_an_overlapping_but_grown_tail_only_counts_new_lines(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy_host(session)
    tail_box: dict[str, list[str]] = {
        "lines": [
            _line("10.0.0.9", "10/Jun/2026:08:42:00 +0000"),
            _line("10.0.0.9", "10/Jun/2026:08:42:01 +0000"),
        ]
    }

    async def _fake_fetch_router_details(settings: Any) -> list[RouterInfo]:
        return [RouterInfo(name="my-router@docker", hostnames={"app.example.com"}, upstream=None)]

    monkeypatch.setattr(gtc_module, "fetch_router_details", _fake_fetch_router_details)

    class _MutableTailAdapter(_FakeTailProxyAdapter):
        async def tail_access_log(self, lines: int = 200) -> list[str] | None:
            return tail_box["lines"]

    collector = GeoTrafficCollector(
        settings=config_module.get_settings(), adapters=_bundle(_MutableTailAdapter(None))
    )

    first = await collector.poll_once()
    assert first == 1

    # The window now contains the two old lines PLUS one brand-new one from
    # a different IP — same overlapping-tail shape a real re-poll produces.
    tail_box["lines"] = [
        *tail_box["lines"],
        _line("10.0.0.8", "10/Jun/2026:08:42:05 +0000"),
    ]
    second = await collector.poll_once()
    assert second == 1  # only the new IP's line counted

    with Session(get_engine()) as s:
        rows = {r.client_ip: r for r in s.exec(select(GeoTrafficSample)).all()}
    # The original IP's count from poll 1 is untouched by poll 2.
    assert rows["10.0.0.9"].hit_count == 2
    assert rows["10.0.0.8"].hit_count == 1


async def test_cursor_is_persisted_in_the_setting_table(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy_host(session)
    lines = [_line("10.0.0.9", "10/Jun/2026:08:42:00 +0000")]
    collector = _collector(lines, monkeypatch)
    await collector.poll_once()

    with Session(get_engine()) as s:
        row = s.get(Setting, CURSOR_SETTING_KEY)
    assert row is not None
    assert row.value["last_ts"] is not None
    assert row.value["last_hashes"]


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


async def test_prune_removes_samples_older_than_retention(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEOIP_TRAFFIC_RETENTION_DAYS", "1")
    config_module.reset_settings_cache()
    ph = _proxy_host(session)
    session.add(
        GeoTrafficSample(
            proxy_host_id=ph.id,
            sampled_at=datetime.now(UTC) - timedelta(days=10),
            client_ip="1.2.3.4",
            bucket="unknown",
            hit_count=1,
        )
    )
    session.commit()

    collector = _collector([], monkeypatch)
    await collector.poll_once()

    with Session(get_engine()) as s:
        rows = s.exec(select(GeoTrafficSample)).all()
    assert rows == []


# ---------------------------------------------------------------------------
# GET /api/traffic/geo
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
    body = r.json()
    assert body == {"enabled": False, "countries": [], "top_ips": [], "sample_count": 0}


def test_geo_endpoint_aggregates_when_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEOIP_ENABLED", "true")
    config_module.reset_settings_cache()

    with Session(get_engine()) as s:
        svc = Service(slug="svc", name="svc")
        s.add(svc)
        s.commit()
        s.refresh(svc)
        ph = ProxyHost(hostname="app.example.com", service_id=svc.id)
        s.add(ph)
        s.commit()
        s.refresh(ph)
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
            GeoTrafficSample(
                proxy_host_id=ph.id,
                client_ip="10.0.0.9",
                bucket="local",
                hit_count=2,
            )
        )
        s.commit()

    r = client.get("/api/traffic/geo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["sample_count"] == 2
    codes = {c["country_code"] or c["bucket"]: c["hits"] for c in body["countries"]}
    assert codes["US"] == 3
    assert codes["local"] == 2
    ips = {i["client_ip"]: i["hits"] for i in body["top_ips"]}
    assert ips["8.8.8.8"] == 3
    assert ips["10.0.0.9"] == 2


def test_geo_endpoint_requires_auth() -> None:
    session_module.create_all_for_tests()
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/traffic/geo")
    assert r.status_code in (401, 403)
