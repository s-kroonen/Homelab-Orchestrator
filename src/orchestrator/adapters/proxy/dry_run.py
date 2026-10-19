"""Log-only proxy adapter for tests, dev, and DRY_RUN mode.

Never makes a real HTTP call, never raises. Mirrors
:class:`~orchestrator.adapters.power.dry_run.DryRunPowerAdapter`'s voice.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestrator.adapters.proxy.base import (
    AccessLogTail,
    EntryPointInfo,
    ProxyAdapter,
    RouteInfo,
    SyncEvent,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class DryRunProxyAdapter(ProxyAdapter):
    def __init__(self) -> None:
        # Routes "written" during this process, so the create/verify/edit flow
        # can be exercised end to end: they show up as healthy runtime routers.
        self._routes: dict[str, RouteInfo] = {}

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

    async def create_route(
        self,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
    ) -> str:
        log.info(
            "proxy.dry_run.create_route",
            hostname=hostname,
            upstream=upstream,
            entry_points=list(entry_points),
            cert_resolver=cert_resolver,
        )
        ref = f"dry-run:{hostname}"
        self._remember(ref, hostname, upstream, entry_points, cert_resolver)
        return ref

    async def update_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
    ) -> None:
        log.info(
            "proxy.dry_run.update_route",
            route_ref=route_ref,
            hostname=hostname,
            upstream=upstream,
            entry_points=list(entry_points),
            cert_resolver=cert_resolver,
        )
        self._remember(route_ref, hostname, upstream, entry_points, cert_resolver)

    def _remember(
        self,
        ref: str,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str,
    ) -> None:
        self._routes[ref] = RouteInfo(
            name=f"{ref}@file",
            hostnames={hostname},
            upstream=upstream,
            entry_points=tuple(entry_points),
            status="enabled",
            cert_resolver=cert_resolver or None,
        )

    async def list_entrypoints(self) -> list[EntryPointInfo] | None:
        # A plausible Traefik: lets the form and validation be exercised
        # without a gateway. Not a claim about any real setup.
        log.info("proxy.dry_run.list_entrypoints")
        return [
            EntryPointInfo("web", ":80"),
            EntryPointInfo("websecure", ":443"),
            EntryPointInfo("traefik", ":8080"),
        ]

    async def list_cert_resolvers(self) -> list[str] | None:
        log.info("proxy.dry_run.list_cert_resolvers")
        return ["letsencrypt"]

    async def delete_route(self, route_ref: str) -> None:
        log.info("proxy.dry_run.delete_route", route_ref=route_ref)
        self._routes.pop(route_ref, None)

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
        return list(self._routes.values())
