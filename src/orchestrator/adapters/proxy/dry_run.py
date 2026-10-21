"""Log-only proxy adapter for tests, dev, and DRY_RUN mode.

Never makes a real HTTP call, never raises. Mirrors
:class:`~orchestrator.adapters.power.dry_run.DryRunPowerAdapter`'s voice.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestrator.adapters.proxy.base import (
    AccessLogTail,
    ConfigFiles,
    EntryPointInfo,
    ProxyAdapter,
    ProxyRouteNotFound,
    RouteInfo,
    RouteOptions,
    SyncEvent,
)
from orchestrator.logging_setup import get_logger
from orchestrator.registry.config_files import normalize_config_file, unknown_file_problem

log = get_logger(__name__)


class DryRunProxyAdapter(ProxyAdapter):
    def __init__(self) -> None:
        # Routes "written" during this process, so the create/verify/edit flow
        # can be exercised end to end: they show up as healthy runtime routers.
        self._routes: dict[str, RouteInfo] = {}
        # Routes switched off: parked here (absent from the runtime list, as
        # Traefik would have them), plus every toggle in call order.
        self._disabled: dict[str, RouteInfo] = {}
        self.toggles: list[tuple[str, bool, bool]] = []
        # Raw (ref -> (hostname, upstream, entry_points, cert_resolver)) so
        # list_routes can describe a route whether it is on or parked.
        self._specs: dict[str, tuple[str, str, tuple[str, ...], str]] = {}
        # Which config file each route "lives in" ('' = the default file).
        self.route_files: dict[str, str] = {}
        # Every create/move in call order, for tests and the dev harness.
        self.created: list[dict[str, object]] = []
        self.moves: list[tuple[str, str]] = []

    async def start(self) -> None:
        log.info("proxy.dry_run.start")

    async def stop(self) -> None:
        log.info("proxy.dry_run.stop")

    def route_ref_for(self, hostname: str) -> str:
        return f"dry-run:{hostname}"

    def owns_route_ref(self, route_ref: str) -> bool:
        return route_ref.startswith("dry-run:")

    async def list_routes(self) -> list[RouteInfo] | None:
        """The routes "written" during this process (on and parked), so the
        push flow can be exercised end to end without a gateway."""
        log.info("proxy.dry_run.list_routes")
        out: list[RouteInfo] = []
        for ref, (hostname, upstream, eps, resolver) in self._specs.items():
            out.append(
                RouteInfo(
                    name=ref,
                    route_id=ref,
                    hostnames={hostname},
                    upstream=upstream,
                    entry_points=eps,
                    cert_resolver=resolver or None,
                    enabled=ref not in self._disabled,
                    config_file=self.route_files.get(ref, ""),
                )
            )
        return out

    async def list_config_files(self) -> ConfigFiles | None:
        # A plausible TM with a config directory: shared middlewares plus
        # whatever routes were "written" here.
        names = {"_shared.yml", *(f for f in self.route_files.values() if f)}
        return ConfigFiles(files=tuple(sorted(names)), can_create=True, default="_shared.yml")

    async def move_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
        options: RouteOptions | None = None,
        config_file: str,
    ) -> str:
        log.info("proxy.dry_run.move_route", route_ref=route_ref, config_file=config_file)
        if route_ref not in self._specs:
            raise ProxyRouteNotFound(f"{route_ref}: not found")
        target = normalize_config_file(config_file)
        self.route_files[route_ref] = target
        self.moves.append((route_ref, target))
        return target

    async def ensure_maintenance_fallback(
        self, *, hostname: str, service_slug: str, raise_errors: bool = False
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
        options: RouteOptions | None = None,
        config_file: str = "",
    ) -> str:
        log.info(
            "proxy.dry_run.create_route",
            hostname=hostname,
            upstream=upstream,
            entry_points=list(entry_points),
            cert_resolver=cert_resolver,
            options=(options or RouteOptions()).to_dict(),
            config_file=config_file,
        )
        cfile = normalize_config_file(config_file)
        problem = unknown_file_problem(cfile, await self.list_config_files())
        if problem:
            raise ValueError(problem)
        ref = self.route_ref_for(hostname)
        self._remember(ref, hostname, upstream, entry_points, cert_resolver)
        self.route_files[ref] = cfile
        self.created.append({"hostname": hostname, "config_file": cfile})
        return ref

    async def update_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
        options: RouteOptions | None = None,
        previous_options: RouteOptions | None = None,
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
        self._specs[ref] = (hostname, upstream, tuple(entry_points), cert_resolver)
        info = RouteInfo(
            name=f"{ref}@file",
            hostnames={hostname},
            upstream=upstream,
            entry_points=tuple(entry_points),
            status="enabled",
            cert_resolver=cert_resolver or None,
        )
        # An edit of a parked route keeps it parked.
        if ref in self._disabled:
            self._disabled[ref] = info
        else:
            self._routes[ref] = info

    async def list_entrypoints(self) -> list[EntryPointInfo] | None:
        # A plausible Traefik: lets the form and validation be exercised
        # without a gateway. Not a claim about any real setup.
        log.info("proxy.dry_run.list_entrypoints")
        return [
            EntryPointInfo("web", ":80"),
            EntryPointInfo("websecure", ":443"),
            EntryPointInfo("traefik", ":8080"),
        ]

    async def list_cert_resolver_sources(self) -> dict[str, list[str]] | None:
        log.info("proxy.dry_run.list_cert_resolver_sources")
        return {"letsencrypt": ["tm_settings"], "letsencrypt-dns": ["static_config"]}

    async def list_domains(self) -> list[str] | None:
        return ["example.com"]

    async def list_middlewares(self) -> list[str] | None:
        return ["redirect-to-https@file"]

    async def delete_route(
        self, route_ref: str, *, with_redirect: bool = True, hostname: str | None = None
    ) -> None:
        log.info("proxy.dry_run.delete_route", route_ref=route_ref)
        self._routes.pop(route_ref, None)
        self._disabled.pop(route_ref, None)
        self._specs.pop(route_ref, None)
        self.route_files.pop(route_ref, None)

    async def set_route_enabled(
        self,
        route_ref: str,
        enabled: bool,
        *,
        with_redirect: bool = False,
        hostname: str | None = None,
    ) -> None:
        log.info(
            "proxy.dry_run.set_route_enabled",
            route_ref=route_ref,
            enabled=enabled,
            with_redirect=with_redirect,
        )
        self.toggles.append((route_ref, enabled, with_redirect))
        if enabled:
            parked = self._disabled.pop(route_ref, None)
            if parked is not None:
                self._routes[route_ref] = parked
        else:
            live = self._routes.pop(route_ref, None)
            if live is not None:
                self._disabled[route_ref] = live

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
