"""``orchestrator.registry.geoip`` — local/private-IP bucketing, resolve()
against a mocked reader, and the disabled/unavailable degrade path."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.config import Settings
from orchestrator.registry.geoip import BUCKET_COUNTRY, BUCKET_LOCAL, BUCKET_UNKNOWN, GeoIP


class _FakeCountry:
    def __init__(self, code: str, name: str) -> None:
        self.iso_code = code
        self.name = name


class _FakeCountryResponse:
    def __init__(self, code: str, name: str) -> None:
        self.country = _FakeCountry(code, name)


class _FakeMetadata:
    def __init__(self, database_type: str) -> None:
        self.database_type = database_type


class _FakeReader:
    """Stands in for ``geoip2.database.Reader`` — only the surface this
    module actually calls."""

    def __init__(
        self, known: dict[str, tuple[str, str]], database_type: str = "DBIP-Country"
    ) -> None:
        self._known = known
        self._database_type = database_type
        self.closed = False

    def metadata(self) -> _FakeMetadata:
        return _FakeMetadata(self._database_type)

    def country(self, ip: str) -> _FakeCountryResponse:
        if ip not in self._known:
            raise LookupError(f"{ip} not found")
        code, name = self._known[ip]
        return _FakeCountryResponse(code, name)

    def close(self) -> None:
        self.closed = True


def _geoip_with_reader(reader: _FakeReader) -> GeoIP:
    """A ``GeoIP`` with an already-"open" fake reader — bypasses
    ``ensure_ready``'s file/network path entirely, same as every other test
    here that only cares about ``resolve()``'s own logic."""
    geoip = GeoIP(Settings(geoip_enabled=True))
    geoip._reader = reader
    geoip._city_capable = "city" in reader._database_type.lower()
    return geoip


# ---------------------------------------------------------------------------
# Local/private/reserved IPs -> a distinct "local" bucket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "127.0.0.1",
        "::1",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
    ],
)
def test_local_and_reserved_ips_bucket_as_local_even_without_a_reader(ip: str) -> None:
    geoip = GeoIP(Settings(geoip_enabled=True))
    lookup = geoip.resolve(ip)
    assert lookup.bucket == BUCKET_LOCAL
    assert lookup.ip == ip


def test_local_bucket_wins_even_with_a_reader_available() -> None:
    reader = _FakeReader({"10.0.0.5": ("XX", "Should never be returned")})
    geoip = _geoip_with_reader(reader)
    lookup = geoip.resolve("10.0.0.5")
    assert lookup.bucket == BUCKET_LOCAL


# ---------------------------------------------------------------------------
# A known public IP resolves against a mocked reader
# ---------------------------------------------------------------------------


def test_known_public_ip_resolves_to_its_country() -> None:
    reader = _FakeReader({"8.8.8.8": ("US", "United States")})
    geoip = _geoip_with_reader(reader)
    lookup = geoip.resolve("8.8.8.8")
    assert lookup.bucket == BUCKET_COUNTRY
    assert lookup.country_code == "US"
    assert lookup.country_name == "United States"


def test_city_capable_database_also_returns_city(monkeypatch: pytest.MonkeyPatch) -> None:
    class _CityResponse:
        def __init__(self) -> None:
            self.country = _FakeCountry("DE", "Germany")
            self.city = type("C", (), {"name": "Berlin"})()

    class _CityReader(_FakeReader):
        def city(self, ip: str) -> Any:
            return _CityResponse()

    reader = _CityReader({}, database_type="DBIP-City-Lite")
    geoip = _geoip_with_reader(reader)
    lookup = geoip.resolve("9.9.9.9")
    assert lookup.bucket == BUCKET_COUNTRY
    assert lookup.country_code == "DE"
    assert lookup.city == "Berlin"


# ---------------------------------------------------------------------------
# Unresolvable public IP / no reader / disabled -> "unknown", never a raise
# ---------------------------------------------------------------------------


def test_public_ip_not_in_database_is_unknown_not_a_crash() -> None:
    reader = _FakeReader({})  # empty - every lookup misses
    geoip = _geoip_with_reader(reader)
    lookup = geoip.resolve("8.8.4.4")
    assert lookup.bucket == BUCKET_UNKNOWN


def test_resolve_without_a_reader_is_unknown() -> None:
    geoip = GeoIP(Settings(geoip_enabled=True))
    lookup = geoip.resolve("8.8.8.8")
    assert lookup.bucket == BUCKET_UNKNOWN


async def test_ensure_ready_is_false_when_disabled() -> None:
    geoip = GeoIP(Settings(geoip_enabled=False))
    assert await geoip.ensure_ready() is False


async def test_ensure_ready_is_false_when_configured_path_is_missing(tmp_path: Any) -> None:
    missing = tmp_path / "does-not-exist.mmdb"
    geoip = GeoIP(Settings(geoip_enabled=True, geoip_db_path=str(missing)))
    assert await geoip.ensure_ready() is False


def test_close_is_idempotent_and_closes_the_reader() -> None:
    reader = _FakeReader({})
    geoip = _geoip_with_reader(reader)
    geoip.close()
    assert reader.closed is True
    geoip.close()  # second call must not raise
