"""Periodic poll of Traefik's Prometheus metrics into :class:`TrafficSample` rows.

Traefik keeps no history of its own request counters — a snapshot is only
ever "total requests since the process last started." This collector is
what turns that into a trend: it polls ``{TRAEFIK_API_URL}/metrics`` on
``TRAEFIK_METRICS_POLL_INTERVAL_S``, maps each live router to the
``ProxyHost`` whose hostname it currently serves (the same hostname lookup
``orchestrator.web.routers.proxy_hosts.resync_proxy_hosts`` already
established), and stores one row per matched router. A router with no
matching ``ProxyHost`` has no ``Service`` to attribute its traffic to and is
silently skipped — the same reasoning as resync's "discovered" list never
being auto-created.

Deliberately a separate, small scheduler from :class:`BackupScheduler`: the
two are unrelated concerns (telemetry polling vs. backup orchestration) with
different failure modes and no shared state, and conflating them would make
either one harder to reason about.

Entirely optional: gated by the same ``TRAEFIK_API_URL`` that gates drift
detection and resync. ``start()`` is a no-op (and never raises) when it is
unset, and ``web/app.py``'s lifespan already treats a failure to start as
non-fatal, the same way it does for :class:`BackupScheduler`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from orchestrator.config import Settings
from orchestrator.db.models import ProxyHost, TrafficSample
from orchestrator.db.session import session_scope
from orchestrator.logging_setup import get_logger
from orchestrator.registry.traefik_status import fetch_router_details, fetch_router_metrics

log = get_logger(__name__)

_JOB_ID = "traefik-traffic-poll"


def hostname_to_proxy_host(session: Session) -> dict[str, ProxyHost]:
    """Every ``ProxyHost``, keyed by its hostname — the lookup side of mapping
    a live Traefik router to the ``Service`` that fronts it."""
    return {ph.hostname: ph for ph in session.exec(select(ProxyHost)).all()}


class TrafficCollector:
    """Owns one APScheduler job: poll Traefik, store samples, prune old ones."""

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._scheduler = AsyncIOScheduler()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start polling. A no-op when Traefik is not configured."""
        if not self._settings.traefik_api_url.strip():
            log.info("traffic_collector.disabled", reason="TRAEFIK_API_URL unset")
            return
        interval_s = max(1, self._settings.traefik_metrics_poll_interval_s)
        self._scheduler.start()
        self._scheduler.add_job(
            self.poll_once,
            "interval",
            seconds=interval_s,
            id=_JOB_ID,
            name="traefik traffic poll",
            next_run_time=datetime.now(self._scheduler.timezone),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(60, interval_s),
        )
        log.info("traffic_collector.started", interval_s=interval_s)

    async def shutdown(self) -> None:
        if self._scheduler.running:
            # Same reasoning as BackupScheduler.shutdown: wait=False so this
            # never blocks container shutdown, then yield once so APScheduler
            # >=3.11's call_soon_threadsafe teardown has actually happened.
            self._scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
        log.info("traffic_collector.stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    # -- the job itself ----------------------------------------------------

    async def poll_once(self) -> int:
        """One poll: fetch metrics, map routers -> ProxyHost, store, prune.

        Never raises — an exception escaping an APScheduler job is logged by
        APScheduler and then forgotten, so every failure mode is caught and
        logged here instead, the same philosophy as the rest of this
        feature. Returns the number of samples inserted (0 on any failure).
        """
        try:
            details = await fetch_router_details(self._settings)
            metrics = await fetch_router_metrics(self._settings)
        except Exception as exc:  # pragma: no cover - defensive; see module docstring
            log.error(
                "traffic_collector.poll_crashed", error=str(exc), error_type=type(exc).__name__
            )
            details = None
            metrics = None

        router_hostnames = {d.name: d.hostnames for d in details} if details else {}

        now = datetime.now(UTC)
        inserted = 0
        try:
            with session_scope() as session:
                if metrics:
                    hostname_map = hostname_to_proxy_host(session)
                    for router_name, sample in metrics.items():
                        hostnames = router_hostnames.get(router_name)
                        if not hostnames:
                            continue
                        proxy_host = None
                        # Sorted for determinism — a router can have several
                        # Host() hostnames, and at most one needs to match.
                        for hostname in sorted(hostnames):
                            proxy_host = hostname_map.get(hostname)
                            if proxy_host is not None:
                                break
                        if proxy_host is None or proxy_host.id is None:
                            continue
                        session.add(
                            TrafficSample(
                                proxy_host_id=proxy_host.id,
                                sampled_at=now,
                                requests_total=sample.requests_total,
                                duration_sum_s=sample.duration_sum_s,
                                duration_count=sample.duration_count,
                                by_code=sample.by_code,
                            )
                        )
                        inserted += 1
                # Pruning is independent of whether this particular poll got
                # any fresh data — a Traefik hiccup must not also stop old
                # samples from eventually being cleaned up.
                self._prune(session)
        except Exception as exc:  # pragma: no cover - defensive
            log.error(
                "traffic_collector.store_failed", error=str(exc), error_type=type(exc).__name__
            )
            return 0

        log.info("traffic_collector.polled", inserted=inserted, routers=len(metrics or {}))
        return inserted

    def _prune(self, session: Session) -> None:
        """Delete samples older than ``TRAEFIK_METRICS_RETENTION_DAYS``. Run as
        part of every poll — simplest way to keep the table bounded without a
        second schedule."""
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.traefik_metrics_retention_days)
        old = session.exec(select(TrafficSample).where(TrafficSample.sampled_at < cutoff)).all()
        for row in old:
            session.delete(row)


__all__ = ["TrafficCollector", "hostname_to_proxy_host"]
