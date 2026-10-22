"""Periodic health-scan cycle — the ONLY place :class:`HealthEngine.scan` is
called with a cache shared across more than one service's own dependency
chain, and the fix for a real inconsistency the owner hit in production.

**The problem.** Before this module existed, ``HealthEngine.scan`` was only
ever invoked from three places — the CLI's manual ``scan`` command,
:class:`orchestrator.pipelines.wake.WakePipeline`, and
:class:`orchestrator.pipelines.backup.BackupPipeline`'s gate check — on no
schedule at all, and each one starting its own fresh ``_cache={}``.
``ServiceState`` (one row per service, holding only the LATEST verdict, no
history) therefore only ever updated as a side effect of something unrelated
happening to touch a service, directly or as someone else's dependency. Worse:
if service A and service B both ``depends_on: [gateway]`` but are scanned at
different real times (say, A during its own backup's gate check, B a minute
later during a wake), each top-level call re-probes "gateway" LIVE at its own
moment via ``_check_dependencies`` — see ``health/engine.py``. A flaky or
slow network path can answer differently each time, so A and B's persisted
``ServiceState`` rows for "gateway" end up disagreeing with each other and
with whatever the dashboard shows for "gateway" itself. This is exactly what
was observed: the gateway showing HEALTHY (from whichever scan last touched
it) while a service depending on it showed BLOCKED/UNKNOWN (because ITS OWN
dependency check re-probed the gateway moments later and got a different live
answer) — not a logic bug in the aggregation rule, a staleness/synchronization
gap between independent, uncoordinated scans.

**The fix.** ``HealthScanner.poll_once`` is one scan cycle over every enabled
service, and it passes the SAME ``cache: dict[str, ServiceVerdict]`` into
every one of those top-level ``engine.scan(...)`` calls — exactly the cache
``HealthEngine._check_dependencies`` already uses internally to avoid
re-probing a dependency already scanned earlier in the SAME call tree, just
now shared across the whole cycle's top-level services too, not only one
service's own dependency chain. A gateway behind N services is therefore
probed exactly ONCE per cycle, and all N dependents — plus the gateway's own
row — agree, by construction, because they all got the exact same verdict
object for that cycle. This does not eliminate staleness (a verdict can still
be up to one cycle old), only the INCONSISTENCY between services about what
the same moment's truth was.

**History.** Each cycle also writes one :class:`HealthScanSample` row per
slug that ended up in the cache that cycle — which naturally includes every
enabled top-level service AND any dependency pulled in along the way, even a
disabled service that something still ``depends_on`` (skipping it would leave
a gap in that dependency chain's history, and ``_check_dependencies`` scans it
regardless of its own ``enabled`` flag, so leaving it out of the history would
be inconsistent with what was actually probed). Old samples are pruned every
cycle, same pattern as :class:`orchestrator.scheduler.traffic_collector
.TrafficCollector._prune` — a short retention on purpose: this is a
monitoring trend view, not an audit trail.

Deliberately a separate, small scheduler, same reasoning as
``TrafficCollector``'s own module docstring: an unrelated concern, with its
own on/off switch, that must not be allowed to share state or a failure mode
with backup scheduling or traffic polling.
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.config import Settings
from orchestrator.db.models import HealthScanSample, Service
from orchestrator.db.session import session_scope
from orchestrator.domain.schemas import ServiceVerdict
from orchestrator.health.engine import HealthEngine
from orchestrator.logging_setup import get_logger
from orchestrator.registry.tree import TreeIndex

log = get_logger(__name__)

_JOB_ID = "health-scan-cycle"

#: Cap kept deliberately generous but bounded — this is "why", not an essay
#: (see HealthScanSample's docstring), and a misbehaving probe should not be
#: able to grow this table's rows without bound.
_REASON_MAX_LEN = 500


class HealthScanner:
    """Owns one APScheduler job: scan every enabled service with one shared
    dependency cache per cycle, store one HealthScanSample per slug touched,
    prune old samples."""

    def __init__(self, *, settings: Settings, adapters: AdapterBundle | None = None) -> None:
        self._settings = settings
        # The Proxmox adapter feeds the guest gate (node online? guest
        # running?). The app always passes the bundle; a bare
        # HealthScanner(settings=) scans without the guest gate.
        self._engine = HealthEngine(
            settings=settings, proxmox=adapters.proxmox if adapters is not None else None
        )
        self._scheduler = AsyncIOScheduler()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start polling. A no-op when ``HEALTH_SCAN_ENABLED`` is false."""
        if not self._settings.health_scan_enabled:
            log.info("health_scanner.disabled", reason="HEALTH_SCAN_ENABLED=false")
            return
        interval_s = max(1, self._settings.health_scan_interval_s)
        self._scheduler.start()
        self._scheduler.add_job(
            self.poll_once,
            "interval",
            seconds=interval_s,
            id=_JOB_ID,
            name="health scan cycle",
            next_run_time=datetime.now(self._scheduler.timezone),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(60, interval_s),
        )
        log.info("health_scanner.started", interval_s=interval_s)

    async def shutdown(self) -> None:
        if self._scheduler.running:
            # Same reasoning as BackupScheduler/TrafficCollector.shutdown:
            # wait=False so this never blocks container shutdown, then yield
            # once so APScheduler >=3.11's call_soon_threadsafe teardown has
            # actually happened before we return.
            self._scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
        log.info("health_scanner.stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    # -- the job itself -------------------------------------------------------

    async def poll_once(self) -> int:
        """One scan cycle. Returns the number of HealthScanSample rows written
        (0 on total failure). Never raises — an exception escaping an
        APScheduler job is logged by APScheduler and then forgotten, so every
        failure mode is caught and logged here instead."""
        cache: dict[str, ServiceVerdict] = {}
        # One Proxmox read of nodes + guests for the whole cycle, same idea as
        # ``cache``: every service is judged against the same moment's
        # cluster state, and N services cost two API calls, not 2N.
        guest_cache = self._engine.new_guest_cache()
        try:
            with session_scope() as session:
                stmt = select(Service).order_by(Service.slug)
                stmt = stmt.where(Service.enabled == True)  # noqa: E712
                services = session.exec(stmt).all()
                tree = TreeIndex.build(session)
                for svc in services:
                    if not tree.is_served(svc):
                        continue  # a disabled ancestor parks its whole subtree
                    if svc.slug in cache:
                        # Already scanned THIS cycle as someone else's
                        # dependency (``_check_dependencies`` populates the
                        # same cache). ``HealthEngine.scan`` itself does not
                        # consult the cache for the service passed to it
                        # directly — only for its dependencies — so without
                        # this check a shared dependency that is ALSO one of
                        # the top-level enabled services would still be
                        # probed a second time whenever the loop reaches it,
                        # defeating the "once per cycle" guarantee.
                        continue
                    try:
                        await self._engine.scan(
                            session, svc, _cache=cache, _guest_cache=guest_cache
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        # One service's probe blowing up must not stop the
                        # rest of the cycle — same philosophy as every other
                        # defensive boundary in this app.
                        log.error(
                            "health_scanner.scan_crashed",
                            service=svc.slug,
                            error=str(exc),
                            error_type=type(exc).__name__,
                            traceback=traceback.format_exc(),
                        )

                inserted = self._store_samples(session, cache)
                # Pruning is independent of whether this cycle's scans
                # succeeded — a probe hiccup must not also stop old samples
                # from eventually being cleaned up. session_scope commits on
                # the way out (engine.scan already committed each verdict
                # individually as it went).
                self._prune(session)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("health_scanner.poll_crashed", error=str(exc), error_type=type(exc).__name__)
            return 0

        log.info("health_scanner.polled", services=len(cache), inserted=inserted)
        return inserted

    def _store_samples(self, session: Session, cache: dict[str, ServiceVerdict]) -> int:
        """Write one HealthScanSample per slug in ``cache`` this cycle.

        ``cache`` holds every slug actually scanned this cycle — every
        enabled top-level service AND any dependency pulled in along the way
        (see the module docstring for why a disabled-but-depended-on service
        is included too)."""
        if not cache:
            return 0
        slug_to_id = {
            s.slug: s.id for s in session.exec(select(Service).where(Service.slug.in_(cache)))
        }
        now = datetime.now(UTC)
        inserted = 0
        for slug, verdict in cache.items():
            service_id = slug_to_id.get(slug)
            if service_id is None:
                # Should not happen (every slug in the cache came from a row
                # we just read), but never let a lookup miss crash the cycle.
                continue
            session.add(
                HealthScanSample(
                    service_id=service_id,
                    sampled_at=now,
                    state=verdict.state,
                    reason=verdict.reason[:_REASON_MAX_LEN],
                )
            )
            inserted += 1
        return inserted

    def _prune(self, session: Session) -> None:
        """Delete samples older than ``health_history_retention_days``. Run as
        part of every cycle — simplest way to keep the table bounded without a
        second schedule, same pattern as TrafficCollector._prune."""
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.health_history_retention_days)
        old = session.exec(
            select(HealthScanSample).where(HealthScanSample.sampled_at < cutoff)
        ).all()
        for row in old:
            session.delete(row)


__all__ = ["HealthScanner"]
