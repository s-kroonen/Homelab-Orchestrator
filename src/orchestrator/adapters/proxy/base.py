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
