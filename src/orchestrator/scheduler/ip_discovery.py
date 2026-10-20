"""Periodic service IP discovery — one :class:`IpDiscoverer` pass over every
enabled service with a Proxmox guest.

Same shape as :class:`~orchestrator.scheduler.health_scanner.HealthScanner`:
its own small APScheduler with its own switch (``IP_DISCOVERY_ENABLED``) and
interval (``IP_DISCOVERY_INTERVAL_S``), never raising out of the job. One
:class:`DiscoveryCycle` per pass, so the guest list, the ARP table (and its
optional sweep) and the DHCP leases are each read once per cycle however many
services there are. Stopped guests are skipped — see ``ipdiscovery/discovery.py``.
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.config import Settings
from orchestrator.db.models import Service
from orchestrator.db.session import session_scope
from orchestrator.domain.enums import GuestKind
from orchestrator.ipdiscovery.discovery import DiscoveryResult, IpDiscoverer
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

_JOB_ID = "ip-discovery-cycle"


class IpDiscoveryScheduler:
    """Owns one APScheduler job: discover every enabled guest-backed service."""

    def __init__(
        self,
        *,
        settings: Settings,
        adapters: AdapterBundle,
        discoverer: IpDiscoverer | None = None,
    ) -> None:
        self._settings = settings
        self._discoverer = discoverer or IpDiscoverer(settings=settings, proxmox=adapters.proxmox)
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        """Start polling. A no-op when ``IP_DISCOVERY_ENABLED`` is false."""
        if not self._settings.ip_discovery_enabled:
            log.info("ip_discovery.disabled", reason="IP_DISCOVERY_ENABLED=false")
            return
        interval_s = max(30, self._settings.ip_discovery_interval_s)
        self._scheduler.start()
        self._scheduler.add_job(
            self.poll_once,
            "interval",
            seconds=interval_s,
            id=_JOB_ID,
            name="service IP discovery",
            next_run_time=datetime.now(self._scheduler.timezone),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(60, interval_s),
        )
        log.info("ip_discovery.started", interval_s=interval_s)

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
        log.info("ip_discovery.stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    async def poll_once(self) -> list[DiscoveryResult]:
        """One pass. Returns the per-service results (empty on total failure).
        Never raises."""
        results: list[DiscoveryResult] = []
        cycle = self._discoverer.new_cycle()
        try:
            with session_scope() as session:
                services = session.exec(
                    select(Service)
                    .where(Service.enabled == True)  # noqa: E712
                    .where(Service.guest_kind != GuestKind.NONE)
                    .where(Service.guest_id != None)  # noqa: E711
                    .order_by(Service.slug)
                ).all()
                for svc in services:
                    try:
                        results.append(await self._discoverer.discover(session, svc, cycle=cycle))
                    except Exception as exc:  # pragma: no cover - defensive
                        session.rollback()
                        log.error(
                            "ip_discovery.service_crashed",
                            service=svc.slug,
                            error=str(exc),
                            traceback=traceback.format_exc(),
                        )
        except Exception as exc:  # pragma: no cover - defensive
            log.error("ip_discovery.poll_crashed", error=str(exc), error_type=type(exc).__name__)
            return results
        log.info(
            "ip_discovery.polled",
            services=len(results),
            found=sum(1 for r in results if r.status == "found"),
            changed=sum(1 for r in results if r.changed),
        )
        return results


__all__ = ["IpDiscoveryScheduler"]
