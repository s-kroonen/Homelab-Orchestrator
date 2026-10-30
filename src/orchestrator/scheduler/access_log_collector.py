"""Periodic poll of Traefik's access log, tailed through Traefik Manager —
the ONE ingestion pipeline behind both ``/traffic`` (per-minute
:class:`TrafficRollup` rows) and its countries (:class:`GeoTrafficSample`
rows, only when ``GEOIP_ENABLED``).

**Why the access log, not Prometheus.** Operators who run Traefik Manager
as their source of truth often leave ``TRAEFIK_API_URL`` empty, and Traefik
Manager does not proxy Traefik's ``/metrics``. The access log is reachable
through Traefik Manager (``GET /api/traefik/logs``) and carries status,
duration, router and (JSON format) the request host — everything both pages
need. :class:`~orchestrator.scheduler.traffic_collector.TrafficCollector`
(Prometheus) still runs for operators who DO set ``TRAEFIK_API_URL``.

**Host attribution, in order** (cached per poll):

1. the request's own host (JSON ``RequestHost``) matched against
   ``ProxyHost.hostname``;
2. the router name, ``@provider`` suffix stripped on both sides, resolved to
   hostnames through the proxy adapter — Traefik Manager's
   ``/api/traefik/routers`` (Traefik's runtime routers, every provider) and
   ``/api/routes`` (the file routes it manages);
3. ``fetch_router_details`` straight from Traefik, only when
   ``TRAEFIK_API_URL`` is set.

A line that resolves to no ``ProxyHost`` is counted under its router in the
status (``unmatched_routers``) and otherwise skipped.

**No native "since" parameter — dedup is this collector's job.** Traefik
Manager always returns the last N lines of the CURRENT file. A persisted
cursor (``Setting`` row :data:`CURSOR_SETTING_KEY`) holds the latest
timestamp seen plus the hashes of lines sharing it; a line is new only if
strictly after the cursor, or equal to it with an unseen hash. The cursor
advances over the whole fetched tail, and survives restarts. If more than
``ACCESS_LOG_LINES`` lines land between two polls the oldest are missed —
an accepted gap (trend visibility, not exact accounting).

**Status, always visible.** Every poll persists a summary in ``Setting``
:data:`STATUS_SETTING_KEY` (last poll time, Traefik Manager's own error
text, lines read/parsed/matched, top unmatched routers, rows written). Both
traffic pages render it, and ``GET /api/traffic/collector-status`` returns
it. "matched"/"unmatched" are measured over the whole fetched tail (not
only the new lines) so the strip stays meaningful on quiet polls.

Gated by ``ACCESS_LOG_ENABLED`` (default true) AND a live (non-dry-run)
proxy adapter. ``start()`` never raises.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, col, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.config import AdapterMode, Settings
from orchestrator.db.models import GeoTrafficSample, ProxyHost, Setting, TrafficRollup
from orchestrator.db.session import session_scope
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.wake import WakePipeline
from orchestrator.pipelines.wake_on_hit import maybe_wake
from orchestrator.registry.access_log import AccessLogEntry, normalize_router_name, parse_lines
from orchestrator.registry.geoip import GeoIP
from orchestrator.registry.traefik_status import fetch_router_details

log = get_logger(__name__)

_JOB_ID = "access-log-poll"

#: Dedup cursor. Keeps the key the GeoIP-only collector used, so upgrading
#: does not re-count whatever is still in the tail window.
CURSOR_SETTING_KEY = "geoip_traffic.cursor"

#: Last poll's summary — see module docstring.
STATUS_SETTING_KEY = "access_log.status"

#: How many unmatched routers the status keeps.
UNMATCHED_TOP_N = 10


def access_log_configured(settings: Settings) -> bool:
    """Would the collector run with these settings? (ACCESS_LOG_ENABLED, a
    live proxy adapter and a Traefik Manager URL.) Used by the pages to pick
    between "not configured" and "no data yet"."""
    return (
        settings.access_log_enabled
        and settings.resolve_adapter(settings.proxy_adapter) is AdapterMode.LIVE
        and bool(settings.traefik_manager_url.strip())
    )


def load_status(session: Session) -> dict[str, Any] | None:
    row = session.get(Setting, STATUS_SETTING_KEY)
    return dict(row.value) if row is not None and row.value else None


def _save_setting(session: Session, key: str, value: dict[str, Any]) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = datetime.now(UTC)
        session.add(row)


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


def _advance_cursor(entries: list[AccessLogEntry], previous: _Cursor) -> _Cursor:
    """The new cursor, derived from EVERY parsed entry in this poll's
    fetched tail (not just the new ones). Never moves backwards."""
    if not entries:
        return previous
    max_ts = max(e.timestamp for e in entries)
    if previous.last_ts is not None and previous.last_ts > max_ts:
        return previous
    tied_hashes = frozenset(_line_hash(e.raw) for e in entries if e.timestamp == max_ts)
    return _Cursor(last_ts=max_ts, last_hashes=tied_hashes)


def _minute(ts: datetime) -> datetime:
    """The UTC minute ``ts`` falls in (tz-aware, like every timestamp here)."""
    return ts.astimezone(UTC).replace(second=0, microsecond=0)


def _status_class(status: int) -> str | None:
    if 200 <= status < 300:
        return "s2xx"
    if 300 <= status < 400:
        return "s3xx"
    if 400 <= status < 500:
        return "s4xx"
    if 500 <= status < 600:
        return "s5xx"
    return None


@dataclass
class _Bucket:
    hits: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    duration_sum: float = 0.0
    duration_count: int = 0
    duration_max: float | None = None

    def add(self, entry: AccessLogEntry) -> None:
        self.hits += 1
        cls = _status_class(entry.status)
        if cls is not None:
            self.counts[cls] += 1
        if entry.duration_ms is not None:
            self.duration_sum += entry.duration_ms
            self.duration_count += 1
            if self.duration_max is None or entry.duration_ms > self.duration_max:
                self.duration_max = entry.duration_ms


def upsert_rollups(session: Session, buckets: dict[tuple[int, datetime], _Bucket]) -> int:
    """ADD each bucket into its ``(proxy_host_id, bucket_start)`` row,
    creating it when missing — a minute can span two polls."""
    written = 0
    for (proxy_host_id, bucket_start), b in buckets.items():
        row = session.exec(
            select(TrafficRollup).where(
                TrafficRollup.proxy_host_id == proxy_host_id,
                TrafficRollup.bucket_start == bucket_start,
            )
        ).one_or_none()
        if row is None:
            row = TrafficRollup(proxy_host_id=proxy_host_id, bucket_start=bucket_start)
        row.hits += b.hits
        row.s2xx += b.counts["s2xx"]
        row.s3xx += b.counts["s3xx"]
        row.s4xx += b.counts["s4xx"]
        row.s5xx += b.counts["s5xx"]
        row.duration_ms_sum += b.duration_sum
        row.duration_ms_count += b.duration_count
        if b.duration_max is not None and (
            row.duration_ms_max is None or b.duration_max > row.duration_ms_max
        ):
            row.duration_ms_max = b.duration_max
        session.add(row)
        written += 1
    return written


def _empty_status(now: datetime) -> dict[str, Any]:
    return {
        "last_poll_at": now.isoformat(),
        "ok": True,
        "error": None,
        "lines_read": 0,
        "parsed": 0,
        "unparsable": 0,
        "new_entries": 0,
        "matched": 0,
        "unmatched_routers": [],
        "rollups_written": 0,
        "geo_samples_written": 0,
        "woken": [],
    }


class AccessLogCollector:
    """Owns one APScheduler job: tail the access log, dedupe, attribute,
    roll up (and GeoIP-sample), prune, persist status."""

    def __init__(self, *, settings: Settings, adapters: AdapterBundle) -> None:
        self._settings = settings
        self._adapters = adapters
        self._geoip = GeoIP(settings)
        self._scheduler = AsyncIOScheduler()
        # Wake-on-hit from the same lines (see _wake_on_hit); needs the power + Proxmox adapters.
        self._wake_pipeline = WakePipeline(
            power=adapters.power, proxmox=adapters.proxmox, settings=settings
        )
        # Polls still running; shutdown cancels them, so none writes after the app is gone.
        self._polls: set[asyncio.Task[Any]] = set()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start polling. A no-op when ACCESS_LOG_ENABLED is false or the
        proxy adapter is the dry-run one (nothing real to tail)."""
        if not self._settings.access_log_enabled:
            log.info("access_log_collector.disabled", reason="ACCESS_LOG_ENABLED=false")
            return
        if isinstance(self._adapters.proxy, DryRunProxyAdapter):
            log.info(
                "access_log_collector.disabled",
                reason="proxy adapter is dry-run (set PROXY_ADAPTER=traefik_manager)",
            )
            return
        interval_s = max(1, self._settings.access_log_poll_interval_s)
        self._scheduler.start()
        self._scheduler.add_job(
            self.poll_once,
            "interval",
            seconds=interval_s,
            id=_JOB_ID,
            name="access log poll",
            next_run_time=datetime.now(self._scheduler.timezone),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(60, interval_s),
        )
        log.info(
            "access_log_collector.started",
            interval_s=interval_s,
            lines=self._settings.access_log_lines,
            geoip=self._settings.geoip_enabled,
        )

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
        polls = list(self._polls)
        for task in polls:
            task.cancel()
        if polls:
            await asyncio.gather(*polls, return_exceptions=True)
        self._geoip.close()
        log.info("access_log_collector.stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    # -- router map ----------------------------------------------------------

    async def _router_map(self) -> dict[str, set[str]]:
        """Normalized router name -> hostnames, from every source available
        this poll (see module docstring). Never raises."""
        out: dict[str, set[str]] = {}

        def _add(name: str, hostnames: set[str]) -> None:
            out.setdefault(normalize_router_name(name), set()).update(hostnames)

        proxy = self._adapters.proxy
        try:
            for info in await proxy.list_runtime_routers() or []:
                _add(info.name, info.hostnames)
        except Exception as exc:  # pragma: no cover - adapters degrade themselves
            log.warning("access_log_collector.runtime_routers_failed", error=str(exc))
        try:
            for info in await proxy.list_routes() or []:
                _add(info.name, info.hostnames)
        except Exception as exc:  # pragma: no cover
            log.warning("access_log_collector.list_routes_failed", error=str(exc))
        if self._settings.traefik_api_url.strip():
            try:
                for detail in await fetch_router_details(self._settings) or []:
                    _add(detail.name, detail.hostnames)
            except Exception as exc:  # pragma: no cover
                log.warning("access_log_collector.traefik_routers_failed", error=str(exc))
        return out

    # -- the job itself ----------------------------------------------------

    async def poll_once(self) -> dict[str, Any]:
        """One poll. Never raises; returns (and persists) the status dict."""
        task = asyncio.current_task()
        if task is not None:
            self._polls.add(task)
        try:
            return await self._poll_and_save()
        finally:
            if task is not None:
                self._polls.discard(task)

    async def _poll_and_save(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        status = _empty_status(now)
        try:
            await self._poll(status)
        except Exception as exc:  # pragma: no cover - defensive
            log.error(
                "access_log_collector.poll_crashed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            status["ok"] = False
            status["error"] = f"{type(exc).__name__}: {exc}"
        try:
            with session_scope() as session:
                _save_setting(session, STATUS_SETTING_KEY, status)
                self._prune(session)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("access_log_collector.status_save_failed", error=str(exc))
        return status

    async def _poll(self, status: dict[str, Any]) -> None:
        try:
            tail = await self._adapters.proxy.fetch_access_log(
                lines=self._settings.access_log_lines
            )
        except Exception as exc:  # pragma: no cover - adapters degrade themselves
            status["ok"] = False
            status["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("access_log_collector.tail_failed", error=status["error"])
            return
        if tail.error is not None:
            status["ok"] = False
            status["error"] = tail.error
            log.warning("access_log_collector.tail_error", error=tail.error)
        raw_lines = tail.lines or []
        status["lines_read"] = len(raw_lines)
        if not raw_lines:
            return

        entries = parse_lines(raw_lines)
        nonblank = sum(1 for line in raw_lines if line.strip())
        status["parsed"] = len(entries)
        status["unparsable"] = nonblank - len(entries)
        if not entries:
            log.warning(
                "access_log_collector.unparsable",
                lines=nonblank,
                sample=next((line for line in raw_lines if line.strip()), "")[:200],
                hint="set Traefik accessLog.format=json (or common)",
            )
            return

        if self._settings.geoip_enabled:
            await self._geoip.ensure_ready()

        with session_scope() as session:
            by_host = {
                ph.hostname.strip().lower(): ph
                for ph in session.exec(select(ProxyHost)).all()
                if ph.id is not None
            }
            router_map: dict[str, set[str]] | None = None
            router_cache: dict[str, ProxyHost | None] = {}

            async def _attribute(entry: AccessLogEntry) -> ProxyHost | None:
                nonlocal router_map
                if entry.host and entry.host in by_host:
                    return by_host[entry.host]
                key = normalize_router_name(entry.router) if entry.router else ""
                if not key:
                    return None
                if key in router_cache:
                    return router_cache[key]
                if router_map is None:
                    router_map = await self._router_map()
                found = None
                for hostname in sorted(router_map.get(key, ())):
                    found = by_host.get(hostname)
                    if found is not None:
                        break
                router_cache[key] = found
                return found

            cursor = _load_cursor(session)
            attributed: list[tuple[AccessLogEntry, ProxyHost | None]] = [
                (e, await _attribute(e)) for e in entries
            ]
            unmatched: Counter[str] = Counter()
            for e, ph in attributed:
                if ph is None:
                    unmatched[e.router or (f"host {e.host}" if e.host else "(no router)")] += 1
            status["matched"] = len(entries) - sum(unmatched.values())
            status["unmatched_routers"] = [
                {"router": name, "hits": hits}
                for name, hits in unmatched.most_common(UNMATCHED_TOP_N)
            ]

            new = [(e, ph) for e, ph in attributed if cursor.is_new(e)]
            status["new_entries"] = len(new)
            new_matched = [(e, ph) for e, ph in new if ph is not None and ph.id is not None]

            buckets: dict[tuple[int, datetime], _Bucket] = {}
            for e, ph in new_matched:
                assert ph is not None and ph.id is not None
                buckets.setdefault((ph.id, _minute(e.timestamp)), _Bucket()).add(e)
            status["rollups_written"] = upsert_rollups(session, buckets)

            if self._settings.geoip_enabled:
                status["geo_samples_written"] = self._store_geo(session, new_matched)

            status["woken"] = self._wake_on_hit(session, new_matched)

            _save_setting(
                session, CURSOR_SETTING_KEY, _advance_cursor(entries, cursor).to_setting()
            )

        if status["matched"] == 0:
            log.warning(
                "access_log_collector.nothing_matched",
                parsed=status["parsed"],
                unmatched_routers=[u["router"] for u in status["unmatched_routers"]],
                hint="each router's hostname needs a proxy host (/proxy-hosts -> Resync)",
            )
        log.info(
            "access_log_collector.polled",
            lines=status["lines_read"],
            new=status["new_entries"],
            matched=status["matched"],
            rollups=status["rollups_written"],
            geo=status["geo_samples_written"],
        )

    def _wake_on_hit(
        self, session: Session, entries: list[tuple[AccessLogEntry, ProxyHost | None]]
    ) -> list[str]:
        """New access-log lines for a proxy host are a hit on it: wake the service behind it when
        the gates allow (``ProxyHost.wake_on_hit`` AND the service's AND the node's switch, see
        :mod:`orchestrator.pipelines.wake_on_hit`). This is the same stand-in the Prometheus
        collector provides, for installs that have no ``TRAEFIK_API_URL``. A failure here never
        affects the rollups. Returns the slugs a wake was started for."""
        hosts: dict[int, ProxyHost] = {}
        for _entry, ph in entries:
            if ph is not None and ph.id is not None and ph.wake_on_hit:
                hosts[ph.id] = ph
        woken: list[str] = []
        for ph in hosts.values():
            try:
                slug = maybe_wake(session, self._wake_pipeline, ph, source="access_log")
            except Exception as exc:  # pragma: no cover - defensive
                log.error(
                    "access_log_collector.wake_on_hit_crashed",
                    proxy_host=ph.hostname,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            if slug:
                woken.append(slug)
        return woken

    def _store_geo(
        self, session: Session, entries: list[tuple[AccessLogEntry, ProxyHost | None]]
    ) -> int:
        """Aggregate by ``(proxy_host_id, client_ip)``, one
        :class:`GeoTrafficSample` per group."""
        now = datetime.now(UTC)
        groups: Counter[tuple[int, str]] = Counter()
        last_status: dict[tuple[int, str], int] = {}
        for entry, ph in entries:
            if ph is None or ph.id is None:
                continue
            key = (ph.id, entry.client_ip)
            groups[key] += 1
            last_status[key] = entry.status
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
        return len(groups)

    def _prune(self, session: Session) -> None:
        now = datetime.now(UTC)
        geo_cutoff = now - timedelta(days=self._settings.geoip_traffic_retention_days)
        for row in session.exec(
            select(GeoTrafficSample).where(col(GeoTrafficSample.sampled_at) < geo_cutoff)
        ).all():
            session.delete(row)
        rollup_cutoff = now - timedelta(days=self._settings.traffic_rollup_retention_days)
        for row in session.exec(
            select(TrafficRollup).where(col(TrafficRollup.bucket_start) < rollup_cutoff)
        ).all():
            session.delete(row)


__all__ = [
    "CURSOR_SETTING_KEY",
    "STATUS_SETTING_KEY",
    "AccessLogCollector",
    "access_log_configured",
    "load_status",
    "upsert_rollups",
]
