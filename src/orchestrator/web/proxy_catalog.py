"""What the live proxy offers (entry points, cert resolvers) - read once and
cached briefly, so opening the add/edit form and validating a save do not
hammer Traefik Manager. A refresh (``?refresh=true``, the form's button, or
Resync) busts the cache."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from orchestrator.adapters.proxy.base import EntryPointInfo, ProxyAdapter
from orchestrator.registry.proxy_validation import resolve_default_entry_points

CACHE_TTL_S = 30.0


@dataclass(frozen=True)
class ProxyCatalog:
    #: ``None`` = could not be read (the form falls back to free text).
    entrypoints: list[EntryPointInfo] | None
    cert_resolvers: list[str] | None
    default_entrypoints: list[str] = field(default_factory=list)
    default_cert_resolver: str = ""
    #: Base domains the operator serves (Traefik Manager's settings) - the form
    #: offers ``domain`` + ``*.domain`` as a ready-made wildcard certificate.
    domains: list[str] = field(default_factory=list)
    #: Name of the HTTP -> HTTPS redirect middleware, and whether Traefik has it
    #: (``None`` = could not tell).
    redirect_middleware: str = "redirect-to-https"
    redirect_available: bool | None = None

    @property
    def source(self) -> str:
        return "traefik_manager" if self.entrypoints is not None else "unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "entrypoints": [{"name": e.name, "address": e.address} for e in self.entrypoints or []],
            "cert_resolvers": list(self.cert_resolvers or []),
            "cert_resolvers_known": self.cert_resolvers is not None,
            "default_entrypoints": self.default_entrypoints,
            "default_cert_resolver": self.default_cert_resolver,
            "domains": list(self.domains),
            "redirect_middleware": self.redirect_middleware,
            "redirect_available": self.redirect_available,
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
    resolvers = await proxy.list_cert_resolvers()
    configured_resolver = (settings.traefik_default_cert_resolver or "").strip()
    if configured_resolver and resolvers is not None and configured_resolver not in resolvers:
        configured_resolver = ""
    domains = await proxy.list_domains()
    middlewares = await proxy.list_middlewares()
    redirect_name = (settings.traefik_redirect_middleware or "redirect-to-https").strip()
    catalog = ProxyCatalog(
        entrypoints=entrypoints,
        cert_resolvers=resolvers,
        default_entrypoints=resolve_default_entry_points(
            settings.default_entry_points_list, entrypoints
        ),
        default_cert_resolver=configured_resolver,
        domains=list(domains or []),
        redirect_middleware=redirect_name,
        redirect_available=(
            None
            if middlewares is None
            else redirect_name in {m.split("@", 1)[0] for m in middlewares}
        ),
    )
    # An unreadable answer is not worth remembering: retry on the next call.
    if entrypoints is not None:
        _cache[key] = (proxy, now, catalog)
    else:
        _cache.pop(key, None)
    return catalog
