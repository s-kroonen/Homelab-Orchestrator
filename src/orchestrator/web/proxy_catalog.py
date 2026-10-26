"""What the live proxy offers (entry points, cert resolvers, config files) - read
once and cached briefly, so opening the add/edit form and validating a save do
not hammer Traefik Manager. A refresh (``?refresh=true``, the form's button,
Resync or the push status) busts the cache."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from orchestrator.adapters.proxy.base import (
    CERT_RESOLVER_SOURCE_LABELS,
    ConfigFiles,
    EntryPointInfo,
    ProxyAdapter,
)
from orchestrator.registry.config_files import (
    DEFAULT_TEMPLATE,
    known_names,
    resolve_config_file,
)
from orchestrator.registry.proxy_validation import resolve_default_entry_points

CACHE_TTL_S = 30.0

RESOLVER_SOURCE_LABELS = CERT_RESOLVER_SOURCE_LABELS


@dataclass(frozen=True)
class ProxyCatalog:
    #: ``None`` = could not be read (the form falls back to free text).
    entrypoints: list[EntryPointInfo] | None
    cert_resolvers: list[str] | None
    default_entrypoints: list[str] = field(default_factory=list)
    default_cert_resolver: str = ""
    #: Where each resolver was seen (``tm_settings`` / ``static_config`` /
    #: ``runtime_router`` / ``default_setting``), in the same order.
    cert_resolver_sources: dict[str, list[str]] = field(default_factory=dict)
    #: Base domains the operator serves (Traefik Manager's settings) - the form
    #: offers ``domain`` + ``*.domain`` as a ready-made wildcard certificate.
    domains: list[str] = field(default_factory=list)
    #: Name of the HTTP -> HTTPS redirect middleware, and whether Traefik has it
    #: (``None`` = could not tell).
    redirect_middleware: str = "redirect-to-https"
    redirect_available: bool | None = None
    #: The same for the HSTS headers middleware.
    hsts_middleware: str = "hsts-headers"
    hsts_available: bool | None = None
    #: Traefik Manager's config files (``None`` = could not be read) and the
    #: ``TRAEFIK_MANAGER_CONFIG_FILE`` template a new route's file comes from.
    config_files: ConfigFiles | None = None
    config_file_template: str = DEFAULT_TEMPLATE
    #: Files routes were seen in (``GET /api/routes``): offered as suggestions
    #: even when the manager's own file list could not be read.
    config_files_seen: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return "traefik_manager" if self.entrypoints is not None else "unavailable"

    def default_config_file(self, service_slug: str | None, hostname: str) -> str:
        """The file a new route for this service/hostname would go into by
        default ("" = Traefik Manager's default file). ``""`` as well when the
        template cannot be resolved for what is given."""
        try:
            return resolve_config_file(
                self.config_file_template, service_slug=service_slug, hostname=hostname
            )
        except ValueError:
            return ""

    def as_dict(
        self, *, service_slug: str | None = None, hostname: str = "example.com"
    ) -> dict[str, Any]:
        files = self.config_files
        return {
            "entrypoints": [{"name": e.name, "address": e.address} for e in self.entrypoints or []],
            "cert_resolvers": list(self.cert_resolvers or []),
            "cert_resolvers_known": self.cert_resolvers is not None,
            # name -> sources that report it; label per source key for tooltips.
            "cert_resolver_sources": {
                n: list(s) for n, s in (self.cert_resolver_sources or {}).items()
            },
            "cert_resolver_source_labels": dict(RESOLVER_SOURCE_LABELS),
            "default_entrypoints": self.default_entrypoints,
            "default_cert_resolver": self.default_cert_resolver,
            "domains": list(self.domains),
            "redirect_middleware": self.redirect_middleware,
            "redirect_available": self.redirect_available,
            "hsts_middleware": self.hsts_middleware,
            "hsts_available": self.hsts_available,
            # Where a new route goes: the known files, whether an unknown name
            # would be created, the template and what it resolves to here.
            "config_files": known_names(files, self.config_files_seen),
            "config_files_known": files is not None,
            "config_files_can_create": files.can_create if files is not None else None,
            "config_file_default": files.default if files is not None else "",
            "config_file_template": self.config_file_template,
            "default_config_file": self.default_config_file(service_slug, hostname),
            "source": self.source,
        }


# adapter -> (monotonic timestamp, catalog). Keyed by the adapter object itself
# (kept alive by the entry) so a test or a restart with a new adapter never sees
# another adapter's answer.
_cache: dict[int, tuple[ProxyAdapter, float, ProxyCatalog]] = {}


def clear_cache() -> None:
    _cache.clear()


async def get_catalog(settings: Any, proxy: ProxyAdapter, *, refresh: bool = False) -> ProxyCatalog:
    key = id(proxy)
    hit = _cache.get(key)
    now = time.monotonic()
    if not refresh and hit is not None and hit[0] is proxy and now - hit[1] < CACHE_TTL_S:
        return hit[2]

    entrypoints = await proxy.list_entrypoints()
    sources = await proxy.list_cert_resolver_sources()
    resolvers = list(sources) if sources is not None else await proxy.list_cert_resolvers()
    configured_resolver = (settings.traefik_default_cert_resolver or "").strip()
    if configured_resolver and resolvers is not None and configured_resolver not in resolvers:
        configured_resolver = ""
    domains = await proxy.list_domains()
    middlewares = await proxy.list_middlewares()
    config_files = await proxy.list_config_files()
    seen: list[str] = []
    if config_files is None or not config_files.files:
        seen = sorted({r.config_file for r in (await proxy.list_routes() or []) if r.config_file})
    redirect_name = (settings.traefik_redirect_middleware or "redirect-to-https").strip()
    hsts_name = (settings.traefik_hsts_middleware or "hsts-headers").strip()
    known_mws = None if middlewares is None else {m.split("@", 1)[0] for m in middlewares}
    catalog = ProxyCatalog(
        entrypoints=entrypoints,
        cert_resolvers=resolvers,
        cert_resolver_sources=dict(sources or {}),
        default_entrypoints=resolve_default_entry_points(
            settings.default_entry_points_list, entrypoints
        ),
        default_cert_resolver=configured_resolver,
        domains=list(domains or []),
        redirect_middleware=redirect_name,
        redirect_available=None if known_mws is None else redirect_name in known_mws,
        hsts_middleware=hsts_name,
        hsts_available=None if known_mws is None else hsts_name in known_mws,
        config_files=config_files,
        config_file_template=getattr(settings, "traefik_manager_config_file", DEFAULT_TEMPLATE),
        config_files_seen=seen,
    )
    # An unreadable answer is not worth remembering: retry on the next call.
    if entrypoints is not None:
        _cache[key] = (proxy, now, catalog)
    else:
        _cache.pop(key, None)
    return catalog
