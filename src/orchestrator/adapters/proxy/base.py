"""Reverse-proxy adapter interface.

Mirrors ``orchestrator.adapters.power``'s shape: an ABC pitched at domain
verbs ("what does drift detection / the maintenance-fallback feature
actually need"), not a 1:1 passthrough of one backend's REST shapes. Today's
only real implementation (:class:`~orchestrator.adapters.proxy.
traefik_manager.TraefikManagerProxyAdapter`) happens to be built on Traefik
Manager's YAML/`errors`-middleware model, but nothing in this interface
assumes that — a future NPM or Pangolin adapter has a completely different
data model (no YAML, no "errors middleware" concept) and must still be able
to implement this ABC.

This formalizes what used to be a standalone pair of modules
(``orchestrator.registry.traefik_manager_client`` /
``traefik_manager_sync``), built when there was only one backend to support.
Now that the actual need is named — room for Traefik Manager today, bare
Traefik, NPM, Pangolin, or something else later, without rewriting every
caller — see ``docs/roadmap.md``'s "generalize the adapter pattern" entry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RouteInfo:
    """One live route this backend currently knows about.

    Provider-neutral: ``name`` is whatever this backend calls that route
    (a Traefik router name, say), ``hostnames`` is every hostname it
    matches, and ``upstream`` is the resolved backend URL(s) — ``None`` when
    the backend reports the route but couldn't resolve what it points at
    (an unsupported load-balancer provider, no servers configured, etc.).
    Mirrors what ``orchestrator.registry.traefik_status.RouterInfo`` already
    provided for the direct-Traefik-API read path, which keeps that exact
    shape for its own callers (see that module).
    """

    name: str
    hostnames: set[str]
    upstream: str | None


@dataclass(frozen=True)
class SyncEvent:
    """One create/update/injection/removal a maintenance-fallback call
    performed, for the caller to audit-log. Mirrors the shape of the old
    ``orchestrator.registry.traefik_manager_sync.MaintenanceSyncEvent`` —
    callers (the proxy-hosts router, the CLI backfill command) feed this
    straight into ``orchestrator.audit.log.record``.
    """

    action: str
    name: str
    details: dict[str, Any] = field(default_factory=dict)


class ProxyRouteNotFound(Exception):
    """Backend-neutral "this route reference no longer exists" signal.

    :meth:`ProxyAdapter.update_route`/:meth:`ProxyAdapter.delete_route`
    implementations raise this (instead of, or wrapping, whatever
    backend-specific "not found" error they'd otherwise raise) so a caller
    (``web/routers/proxy_hosts.py``) can catch exactly this one case —
    "the route Traefik Manager was supposed to have is already gone" —
    without importing a specific adapter implementation's exception
    hierarchy. Every OTHER failure from these two methods should still be a
    real, visible error (see their docstrings) — this is narrowly for the
    one case a caller may reasonably treat as "already done".
    """


class ProxyAdapter(ABC):
    """All methods are async so callers stay non-blocking.

    ``hostname``/``service_slug`` below are the ``ProxyHost.hostname`` and
    the linked ``Service.slug`` — not anything backend-specific.
    """

    # Set False on adapters that are still a stub for a later phase. The
    # factory checks this and substitutes the dry-run implementation rather
    # than handing back an object whose start() raises and crash-loops the
    # container — same convention as ``orchestrator.adapters.power.base.
    # PowerAdapter.IMPLEMENTED``.
    IMPLEMENTED: bool = True

    async def start(self) -> None:  # noqa: B027 - deliberate default no-op, see docstring
        """Open any persistent resource this backend needs. Idempotent.

        Default no-op: every implementation shipped today talks to its
        backend over short-lived, per-call HTTP requests, the same
        reasoning ``orchestrator.registry.traefik_manager_client`` gave for
        not being an adapter in the first place — there is nothing to hold
        open between calls. A future backend that DOES need a persistent
        connection overrides this.
        """

    async def stop(self) -> None:  # noqa: B027 - deliberate default no-op, see start()
        """Close whatever :meth:`start` opened. Idempotent. Default no-op."""

    @abstractmethod
    async def list_routes(self) -> list[RouteInfo] | None:
        """Every live route this backend currently knows about.

        Returns ``None`` — never raises — when this backend is not
        configured or could not be reached, mirroring
        ``orchestrator.registry.traefik_status``'s existing
        degrade-to-"unknown" convention for drift detection (see
        ``docs/proxy_hosts.md``): this is the same read path, just behind
        the adapter now. A caller that needs a hard failure instead of a
        silent degrade (e.g. the admin-triggered Resync action) turns a
        ``None`` result into its own error.
        """

    @abstractmethod
    async def ensure_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        """Ensure a hit on ``hostname`` with a down backend falls back to
        this orchestrator's ``/maintenance/<service_slug>`` page. Idempotent
        — safe to call repeatedly for the same, unchanged pair.

        A backend with no equivalent capability returns ``[]`` and logs a
        warning rather than raising — same fail-open philosophy as the
        rest of this feature (see ``docs/proxy_hosts.md``'s "Fail-open, by
        design" section): this runs as a side effect of an ordinary
        ``ProxyHost`` create/update, never as an explicit admin-triggered
        write, so it must never block that request.
        """

    @abstractmethod
    async def remove_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        """The inverse of :meth:`ensure_maintenance_fallback` — called on a
        ``ProxyHost`` rename (old hostname), re-link (old service) or
        unlink/delete. Same idempotency and fail-open contract."""

    # -- General route CRUD -------------------------------------------------
    #
    # Unlike the maintenance-fallback pair above (a quiet side effect of an
    # unrelated edit, fail-open by design), these three ARE the explicit
    # action the admin just took — creating, editing or deleting a
    # ``ProxyHost`` whose ``router_provider`` is this backend's. A failure
    # here must be visible to the caller, not swallowed: it RAISES on
    # failure rather than degrading to ``None``/``[]``. See
    # ``docs/proxy_hosts.md``'s "Live-managed proxy hosts" section.

    @abstractmethod
    async def create_route(self, *, hostname: str, upstream: str) -> str:
        """Create a real route on this backend for ``hostname`` -> ``upstream``.

        Returns an opaque, backend-specific reference (a route id) the
        caller MUST persist (``ProxyHost.extra``) in order to target this
        exact route again in a later :meth:`update_route`/
        :meth:`delete_route` call — this adapter does not re-derive a route
        from a hostname on its own. Raises on failure (network error,
        backend rejected the request, etc.) rather than returning a
        sentinel; the caller's create must fail visibly too.
        """

    @abstractmethod
    async def update_route(self, route_ref: str, *, hostname: str, upstream: str) -> None:
        """Update the route ``route_ref`` identifies to point at the given
        ``hostname``/``upstream``. Raises on failure, including
        :class:`ProxyRouteNotFound` if the backend no longer has a route
        for that reference (the caller decides what to do about that; see
        :meth:`delete_route`'s note on the same case)."""

    @abstractmethod
    async def delete_route(self, route_ref: str) -> None:
        """Delete the route ``route_ref`` identifies.

        Raises on failure — EXCEPT raises :class:`ProxyRouteNotFound`
        specifically (not a generic failure) when the backend no longer has
        a route for that reference, so a caller deleting its own local
        record can choose to treat "already gone on the backend" as
        success rather than blocking the local delete on it.
        """

    # -- Access-log read path (GeoIP traffic visibility) --------------------

    @abstractmethod
    async def tail_access_log(self, lines: int = 200) -> list[str] | None:
        """The most recent ``lines`` lines of Traefik's own access log, as
        raw strings — one JSON or combined-format log line per entry,
        whichever format Traefik itself is configured to write (see
        ``orchestrator.registry.access_log`` for the parser).

        This is a passive READ, same degrade convention as
        :meth:`list_routes`: returns ``None`` — never raises — when this
        backend is not configured, cannot be reached, or (for
        :class:`~orchestrator.adapters.proxy.traefik_manager
        .TraefikManagerProxyAdapter` specifically) has no access-log path
        configured on the gateway. A caller (``orchestrator.scheduler
        .geo_traffic_collector.GeoTrafficCollector``) treats ``None`` exactly
        like an empty poll: log and skip, same fail-open philosophy as every
        other optional integration here. There is no "since" parameter —
        every poll re-fetches the same tail, and it is the caller's job to
        dedupe against what it already processed (see that collector's
        cursor).
        """
