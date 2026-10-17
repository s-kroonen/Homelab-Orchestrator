"""Periodic poll of Traefik's access log (tailed through Traefik Manager)
into :class:`GeoTrafficSample` rows — the GeoIP companion to
:class:`orchestrator.scheduler.traffic_collector.TrafficCollector`.

**Why a separate collector, not a TrafficCollector extension.** The two read
genuinely different Traefik data: ``TrafficCollector`` polls Traefik's
aggregate Prometheus counters (``{TRAEFIK_API_URL}/metrics``, read straight
from Traefik), which have no per-request client IPs at all. The only place a
client IP appears is the access log, which this app has no direct read
access to — only Traefik Manager does (it already tails it for its own Logs
tab). So this collector goes through a different adapter method
(:meth:`~orchestrator.adapters.proxy.base.ProxyAdapter.tail_access_log`,
Traefik-Manager-backed) and a different gate (``GEOIP_ENABLED``, independent
of ``TRAEFIK_API_URL``) — conflating the two would tie an unrelated feature's
on/off switch and failure mode to traffic metrics' own.

**No native "since" parameter — dedup is this collector's job.** Traefik
Manager's ``GET /api/traefik/logs?lines=N`` always returns the last N lines
of the CURRENT file, with no cursor/offset of its own. Re-polling therefore
re-fetches lines this collector may have already counted. The fix is a
persisted cursor (``Setting`` row, key :data:`CURSOR_SETTING_KEY`): the
LATEST timestamp seen across all parsed lines in the last poll, plus the set
of raw-line hashes that shared that exact timestamp (ties are possible —
Traefik's own access-log timestamp resolution is not guaranteed sub-line-
unique under load). A line only counts as "new" if its timestamp is strictly
after the cursor, OR exactly equal to it and its hash isn't in the recorded
tie-set. The cursor is advanced to the max timestamp (and that timestamp's
line hashes) seen in the CURRENT poll's full fetched tail — not just the
newly-counted lines — so a poll that sees nothing new still keeps the cursor
correct. Persisting it (rather than keeping it only in memory, the way
``TrafficCollector`` derives "previous" from its own previous DB row) means a
container restart does not re-count whatever is still in the tail window.

**Accepted gap, not a bug.** If more than ``lines`` new log lines land
between two polls, the oldest of them fall outside the tail entirely and are
silently missed — there is no way to recover them without reading the file
directly, which is deliberately out of scope (see
``ProxyAdapter.tail_access_log``'s docstring). This mirrors
``TrafficCollector``'s own accepted "up to one poll interval of latency"
trade-off: good enough for trend visibility, not a guarantee of exact counts.

Entirely optional: gated by ``GEOIP_ENABLED`` (default ``False``). ``start()``
is a no-op (never raises) when disabled, and ``web/app.py``'s lifespan treats
a failure to start as non-fatal, same as every other scheduler here.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.config import Settings
from orchestrator.db.models import GeoTrafficSample, ProxyHost, Setting
from orchestrator.db.session import session_scope
from orchestrator.logging_setup import get_logger
from orchestrator.registry.access_log import AccessLogEntry, parse_lines
from orchestrator.registry.geoip import GeoIP
from orchestrator.registry.traefik_status import fetch_router_details

log = get_logger(__name__)

_JOB_ID = "geo-traffic-poll"

#: Setting key the dedup cursor is persisted under. See module docstring.
CURSOR_SETTING_KEY = "geoip_traffic.cursor"


def _line_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class _Cursor:
    last_ts: datetime | None = None
    last_hashes: frozenset[str] = frozenset()

    @classmethod
    def from_setting(cls, value: dict[str, object]) -> _Cursor:
        ts_raw = value.get("last_ts")
        ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else None
        hashes = value.get("last_hashes")
        return cls(
            last_ts=ts,
            last_hashes=frozenset(hashes) if isinstance(hashes, list) else frozenset(),
        )

    def to_setting(self) -> dict[str, object]:
        return {
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "last_hashes": sorted(self.last_hashes),
        }

    def is_new(self, entry: AccessLogEntry) -> bool:
        if self.last_ts is None:
            return True
        if entry.timestamp > self.last_ts:
            return True
        if entry.timestamp == self.last_ts:
            return _line_hash(entry.raw) not in self.last_hashes
        return False


def _load_cursor(session: Session) -> _Cursor:
    row = session.get(Setting, CURSOR_SETTING_KEY)
    if row is None:
        return _Cursor()
    return _Cursor.from_setting(row.value)


def _save_cursor(session: Session, cursor: _Cursor) -> None:
    row = session.get(Setting, CURSOR_SETTING_KEY)
    payload = cursor.to_setting()
    if row is None:
        session.add(Setting(key=CURSOR_SETTING_KEY, value=payload))
    else:
        row.value = payload
        row.updated_at = datetime.now(UTC)


def _advance_cursor(entries: list[AccessLogEntry]) -> _Cursor:
    """The new cursor, derived from EVERY parsed entry in this poll's fetched
    tail (not just the new ones) — see module docstring."""
    if not entries:
        return _Cursor()
    max_ts = max(e.timestamp for e in entries)
    tied_hashes = frozenset(_line_hash(e.raw) for e in entries if e.timestamp == max_ts)
    return _Cursor(last_ts=max_ts, last_hashes=tied_hashes)


def _router_to_proxy_host(
    session: Session, router_hostnames: dict[str, set[str]]
) -> dict[str, ProxyHost]:
    """Router name -> ProxyHost, via the same hostname lookup
    ``TrafficCollector.hostname_to_proxy_host`` already uses, keyed instead
    by router so a parsed access-log entry's ``router`` field resolves
    directly."""
    by_hostname = {ph.hostname: ph for ph in session.exec(select(ProxyHost)).all()}
    out: dict[str, ProxyHost] = {}
    for router, hostnames in router_hostnames.items():
        for hostname in sorted(hostnames):
            proxy_host = by_hostname.get(hostname)
            if proxy_host is not None and proxy_host.id is not None:
                out[router] = proxy_host
                break
    return out


class GeoTrafficCollector:
    """Owns one APScheduler job: tail the access log, dedupe via the
    persisted cursor, resolve IPs through :class:`GeoIP`, aggregate and
    store, prune old samples."""

    def __init__(self, *, settings: Settings, adapters: AdapterBundle) -> None:
        self._settings = settings
        self._adapters = adapters
        self._geoip = GeoIP(settings)
        self._scheduler = AsyncIOScheduler()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start polling. A no-op when ``GEOIP_ENABLED`` is false."""
        if not self._settings.geoip_enabled:
            log.info("geo_traffic_collector.disabled", reason="GEOIP_ENABLED=false")
            return
        interval_s = max(1, self._settings.geoip_traffic_poll_interval_s)
        self._scheduler.start()
        self._scheduler.add_job(
            self.poll_once,
            "interval",
            seconds=interval_s,
            id=_JOB_ID,
            name="geoip traffic poll",
            next_run_time=datetime.now(self._scheduler.timezone),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(60, interval_s),
        )
        log.info("geo_traffic_collector.started", interval_s=interval_s)

    async def shutdown(self) -> None:
        if self._scheduler.running:
            # Same reasoning as every other scheduler here: wait=False so
            # this never blocks container shutdown, then yield once so
            # APScheduler >=3.11's teardown has actually happened.
            self._scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
        self._geoip.close()
        log.info("geo_traffic_collector.stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    # -- the job itself ----------------------------------------------------

    async def poll_once(self) -> int:
        """One poll: tail the log, dedupe, resolve, store, prune. Never
        raises — an exception escaping an APScheduler job is logged by
        APScheduler and then forgotten, so every failure mode is caught and
        logged here instead, same philosophy as the rest of this app.
        Returns the number of samples inserted (0 on any failure/no new
        lines)."""
        try:
            raw_lines = await self._adapters.proxy.tail_access_log(
                lines=self._settings.geoip_access_log_lines
            )
        except Exception as exc:  # pragma: no cover - defensive; adapter degrades itself
            log.error(
                "geo_traffic_collector.tail_failed", error=str(exc), error_type=type(exc).__name__
            )
            raw_lines = None

        if not raw_lines:
            # Either not configured, unreachable, or genuinely empty this
            # poll — pruning still runs below.
            try:
                with session_scope() as session:
                    self._prune(session)
            except Exception as exc:  # pragma: no cover - defensive
                log.error("geo_traffic_collector.prune_failed", error=str(exc))
            return 0

        entries = parse_lines(raw_lines)
        if not entries:
            return 0

        await self._geoip.ensure_ready()

        inserted = 0
        try:
            with session_scope() as session:
                cursor = _load_cursor(session)
                new_entries = [e for e in entries if cursor.is_new(e)]

                if new_entries:
                    router_names = {e.router for e in new_entries}
                    try:
                        details = await fetch_router_details(self._settings)
                    except Exception as exc:  # pragma: no cover - defensive
                        log.error("geo_traffic_collector.router_lookup_failed", error=str(exc))
                        details = None
                    router_hostnames = (
                        {d.name: d.hostnames for d in details if d.name in router_names}
                        if details
                        else {}
                    )
                    router_to_host = _router_to_proxy_host(session, router_hostnames)
                    inserted = self._store(session, new_entries, router_to_host)

                _save_cursor(session, _advance_cursor(entries))
                # Pruning is independent of whether this poll got anything
                # new — a tail-fetch hiccup must not also stop old samples
                # from eventually being cleaned up.
                self._prune(session)
        except Exception as exc:  # pragma: no cover - defensive
            log.error(
                "geo_traffic_collector.store_failed", error=str(exc), error_type=type(exc).__name__
            )
            return 0

        log.info("geo_traffic_collector.polled", inserted=inserted, lines=len(raw_lines))
        return inserted

    def _store(
        self,
        session: Session,
        entries: list[AccessLogEntry],
        router_to_host: dict[str, ProxyHost],
    ) -> int:
        """Aggregate ``entries`` by ``(proxy_host_id, client_ip)`` and write
        one :class:`GeoTrafficSample` per group — entries whose router has
        no matching ``ProxyHost`` are skipped (see class docstring)."""
        now = datetime.now(UTC)
        groups: Counter[tuple[int, str]] = Counter()
        last_status: dict[tuple[int, str], int] = {}
        for entry in entries:
            proxy_host = router_to_host.get(entry.router)
            if proxy_host is None or proxy_host.id is None:
                continue
            key = (proxy_host.id, entry.client_ip)
            groups[key] += 1
            last_status[key] = entry.status

        inserted = 0
        for (proxy_host_id, client_ip), hit_count in groups.items():
            lookup = self._geoip.resolve(client_ip)
            session.add(
                GeoTrafficSample(
                    proxy_host_id=proxy_host_id,
                    sampled_at=now,
                    client_ip=client_ip,
                    bucket=lookup.bucket,
                    country_code=lookup.country_code,
                    country_name=lookup.country_name,
                    city=lookup.city,
                    status_code=last_status[(proxy_host_id, client_ip)],
                    hit_count=hit_count,
                )
            )
            inserted += 1
        return inserted

    def _prune(self, session: Session) -> None:
        """Delete samples older than ``GEOIP_TRAFFIC_RETENTION_DAYS``. Run as
        part of every poll — same pattern as every other collector here."""
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.geoip_traffic_retention_days)
        old = session.exec(
            select(GeoTrafficSample).where(GeoTrafficSample.sampled_at < cutoff)
        ).all()
        for row in old:
            session.delete(row)


__all__ = ["CURSOR_SETTING_KEY", "GeoTrafficCollector"]
