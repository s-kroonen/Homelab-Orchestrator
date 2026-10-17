"""IP -> country/city lookup for the GeoIP traffic-visibility feature.

Wraps the ``geoip2`` library (a thin reader over a MaxMind-format ``.mmdb``
database) behind this app's usual degrade-on-missing-config convention:
disabled or unavailable never raises, it just resolves everything as
"unknown". This is deliberately NOT an :mod:`orchestrator.adapters` adapter
— same reasoning ``orchestrator.registry.traefik_status`` gives for its own
read-only Traefik calls: there is no mutation path, and the one piece of
state worth managing (the open ``geoip2.database.Reader``) is managed here
directly rather than through a ``start``/``stop`` lifecycle, since it is
opened lazily on first use rather than at app startup.

**Database.** ``GEOIP_DB_PATH`` points at a ready-made ``.mmdb`` file.
Left empty (the default), this module downloads and caches the free DB-IP
city-lite database under ``{STATE_DIR}/geoip/`` — the same database Traefik
Manager's own GeoIP feature downloads (``POST /api/geoip/update`` in its
API docs) when no custom database is configured, rather than this app
inventing a different source. The cache is keyed by calendar month
(DB-IP republishes monthly); a stale cached file from a previous month is
still used if a fresh download fails, rather than going dark for a network
hiccup — see :meth:`GeoIP.ensure_ready`.

**Local/private addresses.** A LAN-facing homelab sees plenty of RFC 1918 /
loopback / link-local / CGNAT traffic that no public GeoIP database can
resolve to a country — and *should* not be reported as "unknown" (which
reads as "lookup failed") or silently dropped. These are bucketed as
``"local"`` explicitly (see :meth:`GeoIP.resolve`), distinct from
``"unknown"`` (a public IP the configured database just doesn't cover).
"""

from __future__ import annotations

import gzip
import ipaddress
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: DB-IP's documented free-tier download URL pattern for the city-lite
#: database, republished monthly — the same one Traefik Manager's own
#: ``POST /api/geoip/update`` downloads.
_DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"

#: Local bucket, distinct from "unknown" (see module docstring).
BUCKET_LOCAL = "local"
BUCKET_COUNTRY = "country"
BUCKET_UNKNOWN = "unknown"


@dataclass(frozen=True)
class GeoLookup:
    ip: str
    bucket: str  # "local" | "country" | "unknown"
    country_code: str = ""
    country_name: str = ""
    city: str = ""


def _is_local(ip: str) -> bool:
    """Private/loopback/link-local/CGNAT/reserved/multicast — anything no
    public GeoIP database maps to a country, and that is EXPECTED on a
    homelab LAN rather than a lookup failure."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


class GeoIP:
    """One instance per process (owned by
    :class:`~orchestrator.scheduler.geo_traffic_collector.GeoTrafficCollector`).
    Not thread-safe-sensitive beyond what asyncio's single-threaded event
    loop already gives us — every call here runs on it."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._reader: Any | None = None
        self._city_capable = False

    async def ensure_ready(self) -> bool:
        """Open the database reader if not already open. Returns whether a
        reader is available. Never raises — every failure is logged and
        resolves to ``False``, which makes :meth:`resolve` answer
        ``BUCKET_UNKNOWN`` for every non-local IP, same fail-open
        philosophy as the rest of this feature."""
        if not self._settings.geoip_enabled:
            return False
        if self._reader is not None:
            return True

        path = await self._resolve_db_path()
        if path is None:
            return False

        try:
            import geoip2.database
        except ImportError:
            log.error(
                "geoip.library_missing",
                impact="GEOIP_ENABLED=true but the geoip2 package is not installed.",
            )
            return False

        try:
            reader = geoip2.database.Reader(str(path))
            db_type = reader.metadata().database_type
        except Exception as exc:  # pragma: no cover - defensive, bad/corrupt file
            log.error("geoip.db_open_failed", path=str(path), error=str(exc))
            return False

        self._reader = reader
        self._city_capable = "city" in db_type.lower()
        log.info("geoip.ready", path=str(path), database_type=db_type)
        return True

    def resolve(self, ip: str) -> GeoLookup:
        """Never raises. ``ensure_ready`` must have been called (and
        returned ``True``) for anything but local IPs to resolve to a real
        country — otherwise every non-local IP reads as ``BUCKET_UNKNOWN``."""
        if _is_local(ip):
            return GeoLookup(ip=ip, bucket=BUCKET_LOCAL)
        if self._reader is None:
            return GeoLookup(ip=ip, bucket=BUCKET_UNKNOWN)
        try:
            if self._city_capable:
                resp = self._reader.city(ip)
                city = resp.city.name or ""
            else:
                resp = self._reader.country(ip)
                city = ""
            code = resp.country.iso_code or ""
            name = resp.country.name or ""
        except Exception as exc:  # geoip2.errors.AddressNotFoundError and friends
            log.debug("geoip.lookup_miss", ip=ip, error=str(exc))
            return GeoLookup(ip=ip, bucket=BUCKET_UNKNOWN)
        if not code:
            return GeoLookup(ip=ip, bucket=BUCKET_UNKNOWN)
        return GeoLookup(
            ip=ip, bucket=BUCKET_COUNTRY, country_code=code, country_name=name, city=city
        )

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    # -- database path resolution --------------------------------------------

    async def _resolve_db_path(self) -> Path | None:
        configured = self._settings.geoip_db_path.strip()
        if configured:
            path = Path(configured)
            if not path.is_file():
                log.error(
                    "geoip.configured_db_missing",
                    path=configured,
                    impact="GEOIP_DB_PATH is set but does not exist; GeoIP lookups "
                    "will resolve as unknown until it is fixed. Not "
                    "auto-downloading over an explicit operator choice.",
                )
                return None
            return path
        return await self._cached_or_downloaded()

    def _cache_dir(self) -> Path:
        return self._settings.state_dir / "geoip"

    async def _cached_or_downloaded(self) -> Path | None:
        cache_dir = self._cache_dir()
        month = datetime.now(UTC).strftime("%Y-%m")
        current = cache_dir / f"dbip-city-lite-{month}.mmdb"
        if current.is_file():
            return current

        try:
            downloaded = await self._download(month, current)
            if downloaded is not None:
                return downloaded
        except Exception as exc:  # pragma: no cover - defensive, network errors
            log.warning("geoip.download_failed", month=month, error=str(exc))

        # Fall back to whatever is newest already cached, rather than going
        # dark for a transient network failure (see module docstring).
        existing = sorted(cache_dir.glob("dbip-city-lite-*.mmdb"))
        if existing:
            stale = existing[-1]
            log.warning("geoip.using_stale_db", path=str(stale))
            return stale
        log.warning("geoip.no_db_available", impact="GeoIP lookups resolve as unknown.")
        return None

    async def _download(self, month: str, dest: Path) -> Path | None:
        url = _DBIP_URL.format(month=month)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_gz = dest.with_suffix(".mmdb.gz.tmp")
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            tmp_gz.write_bytes(response.content)
        try:
            with gzip.open(tmp_gz, "rb") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        finally:
            tmp_gz.unlink(missing_ok=True)
        log.info("geoip.downloaded", month=month, path=str(dest))
        return dest


__all__ = ["BUCKET_COUNTRY", "BUCKET_LOCAL", "BUCKET_UNKNOWN", "GeoIP", "GeoLookup"]
