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

**Wake-on-hit (stand-in for a real Traefik-side trigger).** ``POST
/wake/{slug}`` (``web/routers/wake.py``) was designed to be called by
Traefik's own errors middleware whenever a visitor hits a down backend, but
that needs config pushed on the Traefik side, which the owner cannot do yet
(read-only API access only — see ``docs/proxy_hosts.md``). As a stand-in,
each poll here ALSO checks whether traffic landed on a ``ProxyHost`` whose
linked service looks down, and if so fires the same :class:`WakePipeline`
itself — see :meth:`TrafficCollector._check_wake_on_hit` and
``docs/traffic_metrics.md`` for the full three-way gate
(``ProxyHost.wake_on_hit`` AND ``Service.wake_on_hit_enabled`` AND
``Node.wake_on_hit_enabled``), and only for services whose ``wake_trigger`` is
``call`` (a Start-button service is never woken by traffic). This is knowingly slower than a real
Traefik-side trigger — up to one poll interval of latency — which is an
accepted trade-off until write access to Traefik's config exists; injecting
a wake trigger directly into Traefik is future work, out of scope here.
Needs ``adapters`` (an :class:`AdapterBundle`) at construction time to build
its own :class:`WakePipeline`; when omitted (as every pre-existing test
here that doesn't care about wake-on-hit does), this half of the feature is
simply inert — samples are still collected and pruned as before.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, desc, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.config import Settings
from orchestrator.db.models import Node, ProxyHost, Service, ServiceState, TrafficSample
from orchestrator.db.session import session_scope
from orchestrator.domain.enums import HealthState, WakeTrigger
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.wake import WakeError, WakePipeline
from orchestrator.registry.traefik_status import fetch_router_details, fetch_router_metrics
from orchestrator.web.wake_support import fire_background

log = get_logger(__name__)

_JOB_ID = "traefik-traffic-poll"

#: Actor recorded on PipelineRun / audit rows for this path, distinguishing it
#: from actor="api" (the manual/API wake) and actor="scheduler" (backups).
WAKE_ON_HIT_ACTOR = "traefik_hit"


def hostname_to_proxy_host(session: Session) -> dict[str, ProxyHost]:
    """Every ``ProxyHost``, keyed by its hostname — the lookup side of mapping
    a live Traefik router to the ``Service`` that fronts it."""
    return {ph.hostname: ph for ph in session.exec(select(ProxyHost)).all()}


class TrafficCollector:
    """Owns one APScheduler job: poll Traefik, store samples, prune old ones."""

    def __init__(self, *, settings: Settings, adapters: AdapterBundle | None = None) -> None:
        self._settings = settings
        self._scheduler = AsyncIOScheduler()
        # Only needed for the wake-on-hit path (see module docstring). Kept
        # optional so every pre-existing caller/test that only cares about
        # sample collection doesn't have to build a whole AdapterBundle.
        self._adapters = adapters
        self._wake_pipeline = (
            WakePipeline(power=adapters.power, proxmox=adapters.proxmox, settings=settings)
            if adapters is not None
            else None
        )

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
                        # Fetched BEFORE the new row is added, so this is
                        # genuinely the prior poll's snapshot — needed below
                        # to tell "real traffic this poll" from "the host
                        # merely exists."
                        previous = session.exec(
                            select(TrafficSample)
                            .where(TrafficSample.proxy_host_id == proxy_host.id)
                            .order_by(desc(TrafficSample.sampled_at))
                            .limit(1)
                        ).one_or_none()
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

                        # Never let a wake-trigger failure break sample
                        # storage or pruning — same defensive philosophy as
                        # the rest of this module.
                        try:
                            self._check_wake_on_hit(
                                session,
                                proxy_host,
                                previous_total=previous.requests_total if previous else None,
                                current_total=sample.requests_total,
                            )
                        except Exception as exc:  # pragma: no cover - defensive
                            log.error(
                                "traffic_collector.wake_on_hit_crashed",
                                proxy_host=proxy_host.hostname,
                                error=str(exc),
                                error_type=type(exc).__name__,
                            )
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

    def _check_wake_on_hit(
        self,
        session: Session,
        proxy_host: ProxyHost,
        *,
        previous_total: int | None,
        current_total: int,
    ) -> None:
        """Fire the wake pipeline for this poll's ``proxy_host`` when the
        three-way gate, the "already down" check and the "new traffic this
        poll" check all say so. See the module docstring for the design.

        Dedup/cooldown is NOT reinvented here — ``WakePipeline.get_or_start``
        already refuses to start a second run while one is RUNNING, or
        within ``WAKE_RETRY_COOLDOWN_S`` of the last one finishing, which is
        exactly what keeps repeated hits during an outage from spamming wake
        attempts. This method only decides WHETHER to call it at all.
        """
        if self._wake_pipeline is None:
            return  # no AdapterBundle given — wake-on-hit is inert.

        if not proxy_host.wake_on_hit:
            return

        # A brand-new host has nothing to compare against yet; a decrease
        # means Traefik's own counter reset (it restarted), which is "no
        # delta available," never "negative traffic" — see TrafficSample's
        # docstring. Either way, no NEW traffic is confirmed this poll.
        if previous_total is None or current_total <= previous_total:
            return

        if proxy_host.service_id is None:
            return
        service = session.get(Service, proxy_host.service_id)
        if service is None or service.id is None or not service.wake_on_hit_enabled:
            return
        if service.wake_trigger is not WakeTrigger.CALL:
            return  # a Start-button service is never woken by traffic alone

        if service.node_id is None:
            return
        node = session.get(Node, service.node_id)
        if node is None or not node.wake_on_hit_enabled:
            return

        if not self._is_down(session, service):
            return

        try:
            run, started_new = self._wake_pipeline.get_or_start(
                session, service.slug, actor=WAKE_ON_HIT_ACTOR
            )
        except WakeError as exc:
            # Disabled service, no node, etc. — nothing to do; the manual
            # wake path would hit the same error, this just never surfaces
            # it to anyone.
            log.warning(
                "traffic_collector.wake_on_hit_skipped",
                service=service.slug,
                proxy_host=proxy_host.hostname,
                error=str(exc),
            )
            return

        if started_new:
            log.info(
                "traffic_collector.wake_on_hit_triggered",
                service=service.slug,
                proxy_host=proxy_host.hostname,
                run_id=run.id,
            )
            fire_background(self._wake_pipeline, run, service.slug, actor=WAKE_ON_HIT_ACTOR)

    @staticmethod
    def _is_down(session: Session, service: Service) -> bool:
        """ "Down" for wake-on-hit purposes: a definite FAILED verdict, or no
        verdict has ever been recorded at all (the service has never been
        scanned, so there is no evidence it is up — safe to try, and
        ``get_or_start``'s cooldown stops this from being tried repeatedly).

        Deliberately NOT true for an existing UNKNOWN verdict: that can mean
        a blocked dependency, a probe/transport config error, or a transient
        network blip unrelated to the node's power state — treating it as
        "down" would auto-wake services on signals that have nothing to do
        with whether the node is actually off, which is a much higher-cost
        mistake here than for the backup gate UNKNOWN already serves (no
        backup vs. an unwanted power-on + guest start).

        DOWN (the guest gate: node offline or guest not running) counts too —
        it is the most direct evidence there is that a wake is what's needed.
        """
        state = session.get(ServiceState, service.id)
        if state is None:
            return True
        return state.last_verdict in (HealthState.FAILED, HealthState.DOWN)

    def _prune(self, session: Session) -> None:
        """Delete samples older than ``TRAEFIK_METRICS_RETENTION_DAYS``. Run as
        part of every poll — simplest way to keep the table bounded without a
        second schedule."""
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.traefik_metrics_retention_days)
        old = session.exec(select(TrafficSample).where(TrafficSample.sampled_at < cutoff)).all()
        for row in old:
            session.delete(row)


__all__ = ["WAKE_ON_HIT_ACTOR", "TrafficCollector", "hostname_to_proxy_host"]
