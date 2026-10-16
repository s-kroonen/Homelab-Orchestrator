"""Log-only proxy adapter for tests, dev, and DRY_RUN mode.

Never makes a real HTTP call, never raises. Mirrors
:class:`~orchestrator.adapters.power.dry_run.DryRunPowerAdapter`'s voice.
"""

from __future__ import annotations

from orchestrator.adapters.proxy.base import ProxyAdapter, RouteInfo, SyncEvent
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class DryRunProxyAdapter(ProxyAdapter):
    async def start(self) -> None:
        log.info("proxy.dry_run.start")

    async def stop(self) -> None:
        log.info("proxy.dry_run.stop")

    async def list_routes(self) -> list[RouteInfo] | None:
        log.info("proxy.dry_run.list_routes")
        return []

    async def ensure_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        log.info(
            "proxy.dry_run.ensure_maintenance_fallback",
            hostname=hostname,
            service_slug=service_slug,
        )
        return [
            SyncEvent(
                "middleware.injected",
                f"maintenance-{service_slug}",
                {"hostname": hostname, "dry_run": True},
            )
        ]

    async def remove_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        log.info(
            "proxy.dry_run.remove_maintenance_fallback",
            hostname=hostname,
            service_slug=service_slug,
        )
        return [
            SyncEvent(
                "middleware.removed",
                f"maintenance-{service_slug}",
                {"hostname": hostname, "dry_run": True},
            )
        ]
