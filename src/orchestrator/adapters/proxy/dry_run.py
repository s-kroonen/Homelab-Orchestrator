"""Log-only proxy adapter for tests, dev, and DRY_RUN mode.

Never makes a real HTTP call, never raises. Mirrors
:class:`~orchestrator.adapters.power.dry_run.DryRunPowerAdapter`'s voice.
"""

from __future__ import annotations

from orchestrator.adapters.proxy.base import AccessLogTail, ProxyAdapter, RouteInfo, SyncEvent
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

    async def create_route(self, *, hostname: str, upstream: str) -> str:
        log.info("proxy.dry_run.create_route", hostname=hostname, upstream=upstream)
        return f"dry-run:{hostname}"

    async def update_route(self, route_ref: str, *, hostname: str, upstream: str) -> None:
        log.info(
            "proxy.dry_run.update_route", route_ref=route_ref, hostname=hostname, upstream=upstream
        )

    async def delete_route(self, route_ref: str) -> None:
        log.info("proxy.dry_run.delete_route", route_ref=route_ref)

    async def tail_access_log(self, lines: int = 200) -> list[str] | None:
        # Deliberately ``None``, not a fabricated ``[]`` or fake log lines:
        # unlike the write methods above (which fake a plausible SyncEvent so
        # callers exercising the dry-run path see something), there is no
        # real backend here with a real access log to stand in for, and
        # fabricating IP/country-shaped data would be actively misleading
        # for a feature whose whole point is "what real traffic looks like."
        # Mirrors list_routes' "not configured" convention instead — never
        # touches the network, never raises.
        log.info("proxy.dry_run.tail_access_log")
        return None

    async def fetch_access_log(self, lines: int = 200) -> AccessLogTail:
        log.info("proxy.dry_run.fetch_access_log")
        return AccessLogTail(
            lines=None, error="Dry-run proxy adapter: there is no real access log to read."
        )

    async def list_runtime_routers(self) -> list[RouteInfo] | None:
        log.info("proxy.dry_run.list_runtime_routers")
        return []
