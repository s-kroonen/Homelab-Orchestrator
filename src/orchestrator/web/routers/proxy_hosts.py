"""Proxy-host CRUD — the DB-backed web equivalent of ``orchestrator-cli proxy``.

A ``ProxyHost`` whose ``router_provider`` is ``traefik`` is now
**live-managed**: creating/editing/deleting it here creates/edits/deletes
the actual route on Traefik Manager too (``POST``/``PUT``/``DELETE`` below
write-through to ``adapters.proxy.create_route``/``update_route``/
``delete_route``, raising a clear error rather than silently leaving the DB
and the gateway disagreeing). A ``ProxyHost`` whose ``router_provider`` is
``pangolin``/``npm``/``other`` is still a plain reference record — no
adapter exists for those backends yet, so those rows are DB-only, same as
every ``ProxyHost`` used to be. See ``docs/proxy_hosts.md``'s "Live-managed
proxy hosts" section for the full write-through/rollback/backfill
behaviour. The ``live_status`` field computed into every response (see
``orchestrator.registry.traefik_status``) and ``POST /resync`` below
remain read-only/bulk-discovery, as before — see that doc for how Resync's
role changes now that write-through exists.

``proxy_host`` is YAML-owned (``docs/architecture.md``), but this router only
touches the DB, the same way every other dashboard edit does: the existing
``POST /api/registry/save`` / ``/api/registry/reset`` endpoints round-trip
the live DB to ``services.yaml``. The YAML schema nests proxy hosts under
their service, so ``service_id`` is required here too — matching
``orchestrator-cli proxy add <slug>``, which always takes one. Without this,
a proxy host with no service would silently never reach the saved YAML.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.proxy.base import (
    ProxyRouteNotFound,
    RouteInfo,
    RouteOptions,
    SyncEvent,
)
from orchestrator.adapters.proxy.traefik_manager import is_plain_http_entrypoint
from orchestrator.adapters.proxy.verify import (
    ERROR,
    NOT_LOADED,
    UNVERIFIED,
    RouteVerification,
    bare_router_name,
    match_runtime_router,
    router_state,
    verify_route,
)
from orchestrator.audit import log as audit
from orchestrator.db.models import ProxyHost, Service
from orchestrator.domain.enums import RouterProvider
from orchestrator.logging_setup import get_logger
from orchestrator.registry.proxy_validation import (
    normalize_hostname,
    parse_entry_points,
    validate_proxy_host,
)
from orchestrator.registry.traefik_probes import ProbeSyncEvent, sync_traefik_probe
from orchestrator.registry.traefik_status import (
    drift_status,
    fetch_router_details,
    fetch_router_hostnames,
)
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.proxy_catalog import ProxyCatalog, get_catalog

log = get_logger(__name__)

#: The ``ProxyHost.extra`` key a live-managed route's opaque backend
#: reference is stored under (today, the route name
#: ``ProxyAdapter.create_route`` returned from Traefik Manager — see
#: ``orchestrator.adapters.proxy.traefik_manager.route_name_for_hostname``).
#: A later update/delete reads this back rather than re-deriving a route
#: from the hostname, since a hostname rename does NOT rename the
#: underlying route. See ``docs/proxy_hosts.md``'s "Live-managed proxy
#: hosts" section.
TRAEFIK_MANAGER_ROUTE_REF_KEY = "traefik_manager_route_id"


@dataclass
class LiveState:
    """One read of what the proxy has right now, shared by a whole response."""

    #: Hostnames some router covers; ``None`` = unknown (poll failed / unset).
    hostnames: set[str] | None
    #: Traefik Manager's own routes (``/api/routes``), when readable.
    routes: list[RouteInfo] | None
    #: Traefik's runtime routers with status/error, when readable.
    runtime: list[RouteInfo] | None


async def _live_state(settings: Any, adapters: AdapterBundle) -> LiveState:
    routes: list[RouteInfo] | None = None
    if settings.traefik_manager_url.strip():
        routes = await adapters.proxy.list_routes()
        if routes is None:
            hostnames = None
        else:
            hostnames = set()
            for route in routes:
                hostnames.update(route.hostnames)
    else:
        hostnames = await fetch_router_hostnames(settings)
    runtime = await adapters.proxy.list_runtime_routers()
    return LiveState(hostnames=hostnames, routes=routes, runtime=runtime)


async def _live_hostnames(settings: Any, adapters: AdapterBundle) -> set[str] | None:
    """Read-path entry point for drift detection (``live_status``).

    Prefers the ``ProxyAdapter`` (Traefik Manager today) when
    ``TRAEFIK_MANAGER_URL`` is configured. Falls back to polling Traefik's
    own API directly (``TRAEFIK_API_URL``) when Traefik Manager isn't
    configured — kept rather than retired outright because (a) it costs
    nothing to keep working, (b) an operator may run this orchestrator
    against a gateway that still only exposes Traefik's bare API without
    Traefik Manager in front of it, and (c) the existing test suite and
    docs exercise it directly. See docs/proxy_hosts.md for the full
    reasoning. Both paths degrade to ``None`` ("unknown") on any failure —
    never raised into a page load.
    """
    if settings.traefik_manager_url.strip():
        routes = await adapters.proxy.list_routes()
        if routes is None:
            return None
        hostnames: set[str] = set()
        for route in routes:
            hostnames.update(route.hostnames)
        return hostnames
    return await fetch_router_hostnames(settings)


async def _router_details_or_503(settings: Any, adapters: AdapterBundle) -> list[RouteInfo]:
    """Read-path entry point for Resync — same TM-preferred/API-fallback
    priority as :func:`_live_hostnames`, but raises a clear ``503`` instead
    of degrading, matching Resync's existing "this is a manual action the
    admin is watching" behaviour (see docs/proxy_hosts.md).
    """
    if settings.traefik_manager_url.strip():
        routers = await adapters.proxy.list_routes()
        source = "Traefik Manager (TRAEFIK_MANAGER_URL)"
    else:
        routers = await fetch_router_details(settings)
        source = "Traefik (TRAEFIK_API_URL)"
    if routers is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{source} is not configured or could not be reached — see "
            "docs/proxy_hosts.md#resync-manual-admin-triggered-write.",
        )
    return routers


async def _sync_maintenance_fallback(
    adapters: AdapterBundle,
    *,
    hostname: str | None,
    service_slug: str | None,
    old_hostname: str | None = None,
    old_service_slug: str | None = None,
) -> list[SyncEvent]:
    """Lifecycle orchestration around ``ProxyAdapter.ensure/remove_
    maintenance_fallback`` — create/rename/re-link/unlink/delete, exactly
    the same shape ``orchestrator.registry.traefik_manager_sync.
    sync_maintenance_middleware`` used to implement itself (see its
    docstring, preserved here since the adapter's two methods are
    deliberately simpler single-purpose verbs and don't know about "old"
    values themselves).

    Call with the ``ProxyHost``'s CURRENT ``(hostname, service_slug)`` and,
    when either changed, the PREVIOUS values. No-op, returns ``[]``, when
    there is nothing to do (everything — new and old — is unset).
    """
    if old_service_slug is None and old_hostname is None:
        old_service_slug, old_hostname = service_slug, hostname
    if service_slug is None and old_service_slug is None:
        return []

    events: list[SyncEvent] = []
    moved_or_cleared = old_service_slug is not None and old_service_slug != service_slug
    renamed_in_place = (
        not moved_or_cleared
        and old_hostname is not None
        and hostname is not None
        and old_hostname != hostname
    )
    if (moved_or_cleared or renamed_in_place) and old_service_slug and old_hostname:
        events.extend(
            await adapters.proxy.remove_maintenance_fallback(
                hostname=old_hostname, service_slug=old_service_slug
            )
        )

    if service_slug is None or not hostname:
        return events
    events.extend(
        await adapters.proxy.ensure_maintenance_fallback(
            hostname=hostname, service_slug=service_slug
        )
    )
    return events


router = APIRouter(
    prefix="/api/proxy-hosts", tags=["proxy-hosts"], dependencies=[Depends(require_admin)]
)


class ProxyHostIn(BaseModel):
    """Create/replace payload. PUT takes the full record, like POST.

    ``entry_points`` / ``cert_resolver`` left out (``None``) mean "not chosen
    here": a create resolves them from the configured default (or the live
    Traefik), an edit keeps what is stored (or what the route already has).
    They are validated and written explicitly - never left for Traefik Manager
    to default.
    """

    hostname: str
    upstream: str = ""
    router_provider: RouterProvider = RouterProvider.TRAEFIK
    service_id: int
    extra: dict[str, Any] = Field(default_factory=dict)
    #: See ProxyHost.wake_on_hit — per-route opt-in for the
    #: TrafficCollector-driven "wake on hit" stand-in. Defaults False.
    wake_on_hit: bool = False
    entry_points: list[str] | None = None
    cert_resolver: str | None = None
    #: See adapters.proxy.base.RouteOptions. ``None`` = keep what is stored
    #: (an edit) or use the defaults (a create): pass the Host header through,
    #: verify the upstream's TLS, no wildcard domains, no HTTP -> HTTPS router.
    route_options: dict[str, Any] | None = None
    #: Record a route Traefik Manager ALREADY has (Resync's "Discovered") without
    #: writing to it. Never creates, never overwrites.
    adopt: bool = False

    @field_validator("hostname", "upstream")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("entry_points", mode="before")
    @classmethod
    def _parse_entry_points(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str | list | tuple):
            return parse_entry_points(v)
        return v


def _fail(
    code: int,
    message: str,
    errors: dict[str, str] | None = None,
    **extra: Any,
) -> HTTPException:
    """A ``{field: message}`` error body: ``detail = {"message", "errors"}``."""
    return HTTPException(code, detail={"message": message, "errors": errors or {}, **extra})


def _traefik_block(
    ph: ProxyHost, live: LiveState | None, expect: Sequence[str] | None = None
) -> dict[str, Any] | None:
    """Per-host Traefik status for list/get/resync: the runtime router's own
    status and error text (what Traefik Manager shows)."""
    if ph.router_provider is not RouterProvider.TRAEFIK:
        return None
    if live is None or live.runtime is None:
        return RouteVerification(
            UNVERIFIED, "Traefik's runtime routers are not readable."
        ).as_dict()
    ref = (ph.extra or {}).get(TRAEFIK_MANAGER_ROUTE_REF_KEY)
    info = match_runtime_router(live.runtime, route_ref=ref, hostname=ph.hostname)
    state = router_state(info, expect_entry_points=expect)
    out = state.as_dict()
    if info is None and live.routes:
        # Not in Traefik's runtime list, but Traefik Manager knows the route:
        # still surface the entry points it has on file.
        route = match_runtime_router(live.routes, route_ref=ref, hostname=ph.hostname)
        if route is not None:
            out["entry_points"] = list(getattr(route, "entry_points", ()))
    return out


def _serialize(
    ph: ProxyHost,
    services: dict[int, Service],
    live_hostnames: set[str] | None,
    live: LiveState | None = None,
) -> dict[str, Any]:
    svc = services.get(ph.service_id)
    return {
        "id": ph.id,
        "hostname": ph.hostname,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
        "service_slug": svc.slug if svc else None,
        "service_name": svc.name if svc else None,
        "extra": ph.extra or {},
        "wake_on_hit": ph.wake_on_hit,
        "entry_points": list(ph.entry_points or []),
        "cert_resolver": ph.cert_resolver or "",
        "route_options": RouteOptions.from_dict(ph.route_options).to_dict(),
        # Empty list = created before entry points were stored; the edit form
        # then pre-selects from `traefik.entry_points` (what the route has).
        "entry_points_stored": bool(ph.entry_points),
        "last_seen_at": ph.last_seen_at.isoformat() if ph.last_seen_at else None,
        # Computed fresh on each request, never persisted — see
        # orchestrator.registry.traefik_status. "unknown" means Traefik polling
        # is not configured or the poll failed, NOT that the route is missing.
        "live_status": drift_status(ph.hostname, live_hostnames).value,
        # Traefik's verdict on the router itself (state ok / error / not_loaded
        # / unverified, with Traefik's own error text when it is broken).
        "traefik": _traefik_block(ph, live),
    }


def _services_by_id(session: Session) -> dict[int, Service]:
    return {s.id: s for s in session.exec(select(Service)).all() if s.id is not None}


def _audit_probe_sync_events(
    session: Session,
    admin: CurrentAdmin,
    events: list[ProbeSyncEvent],
    services: dict[int, Service] | None = None,
) -> None:
    """Audit each auto-managed Traefik probe create/update/delete.

    Same ``orchestrator.audit.log.record`` mechanism, and the same
    ``probe.created`` / ``probe.updated`` / ``probe.deleted`` action names,
    as the hand-written probe CRUD in ``web/routers/dashboard.py`` — this is
    not a new action vocabulary, just a different trigger for it.
    """
    if not events:
        return
    services = services if services is not None else _services_by_id(session)
    for event in events:
        svc = services.get(event.service_id)
        slug = svc.slug if svc else str(event.service_id)
        details: dict[str, Any] = {
            "probe_id": event.probe_id,
            "name": event.name,
            "source": "traefik",
        }
        if event.config is not None:
            details["config"] = event.config
        audit.record(
            session,
            actor=admin.username,
            action=f"probe.{event.action}",
            target=f"service:{slug}",
            details=details,
        )


def _audit_maintenance_sync_events(
    session: Session, admin: CurrentAdmin, events: list[SyncEvent]
) -> None:
    """Audit each maintenance-middleware create/update/injection/removal.

    Same ``orchestrator.audit.log.record`` mechanism as every other mutation
    in this file. Action names are ``middleware.<action>`` /
    ``backend_service.<action>`` — see
    ``orchestrator.adapters.proxy.base.SyncEvent``.
    """
    for event in events:
        audit.record(
            session,
            actor=admin.username,
            action=event.action,
            target=f"traefik_manager:{event.name}",
            details=event.details,
        )


async def _write_through_create(
    adapters: AdapterBundle,
    *,
    hostname: str,
    upstream: str,
    entry_points: Sequence[str],
    cert_resolver: str,
    options: RouteOptions | None = None,
) -> str:
    """Create the real route on Traefik Manager for a new, live-managed
    (``router_provider == traefik``) ``ProxyHost``. Raises a ``502`` — the
    caller must not commit the local row when this raises (see
    ``docs/proxy_hosts.md``'s "Live-managed proxy hosts" section)."""
    try:
        return await adapters.proxy.create_route(
            hostname=hostname,
            upstream=upstream,
            entry_points=entry_points,
            cert_resolver=cert_resolver,
            options=options,
        )
    except HTTPException:  # pragma: no cover - defensive, adapters don't raise this
        raise
    except Exception as exc:
        log.warning("proxy_host.live_route_create_failed", hostname=hostname, error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"could not create the route on Traefik Manager: {exc}",
        ) from exc


async def _write_through_update(
    adapters: AdapterBundle,
    ph: ProxyHost,
    *,
    hostname: str,
    upstream: str,
    entry_points: Sequence[str],
    cert_resolver: str,
    options: RouteOptions | None = None,
) -> tuple[str, bool]:
    """Live-managed update write-through for ``PUT``. Returns ``(route_ref,
    created)``.

    If ``ph.extra`` has no stored route reference yet — a host that
    predates this feature, or one whose ``router_provider`` is only just
    now switching to ``traefik`` — a route is CREATED instead of erroring.
    The same backfill-by-creating happens if the stored reference no
    longer resolves on the backend (:class:`ProxyRouteNotFound` — e.g. the
    route was deleted by hand on Traefik Manager since). See
    ``docs/proxy_hosts.md``'s "Live-managed proxy hosts" section.
    """
    existing_ref = (ph.extra or {}).get(TRAEFIK_MANAGER_ROUTE_REF_KEY)
    if existing_ref:
        try:
            await adapters.proxy.update_route(
                existing_ref,
                hostname=hostname,
                upstream=upstream,
                entry_points=entry_points,
                cert_resolver=cert_resolver,
                options=options,
                previous_options=RouteOptions.from_dict(ph.route_options),
            )
            return str(existing_ref), False
        except ProxyRouteNotFound:
            log.info(
                "proxy_host.live_route_missing_backfilling",
                route_ref=existing_ref,
                hostname=hostname,
            )
        except Exception as exc:
            log.warning("proxy_host.live_route_update_failed", hostname=hostname, error=str(exc))
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"could not update the route on Traefik Manager: {exc}",
            ) from exc
    ref = await _write_through_create(
        adapters,
        hostname=hostname,
        upstream=upstream,
        entry_points=entry_points,
        cert_resolver=cert_resolver,
        options=options,
    )
    return ref, True


async def _write_through_delete(
    adapters: AdapterBundle, route_ref: str, *, with_redirect: bool = False
) -> None:
    """Delete the real route for a live-managed ``ProxyHost`` being deleted.

    A :class:`ProxyRouteNotFound` (the route is already gone on the
    backend) is logged and swallowed — the operator's intent ("this host
    should no longer exist") is still satisfiable from the local side.
    Every other failure raises a ``502`` and blocks the local delete."""
    try:
        await adapters.proxy.delete_route(route_ref, with_redirect=with_redirect)
    except ProxyRouteNotFound:
        log.info("proxy_host.live_route_already_gone", route_ref=route_ref)
    except Exception as exc:
        log.warning("proxy_host.live_route_delete_failed", route_ref=route_ref, error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"could not delete the route on Traefik Manager: {exc}",
        ) from exc


def _is_live_managed(body: ProxyHostIn) -> bool:
    """A ``ProxyHost`` write-through to the real backend applies only when
    ALL hold: ``router_provider`` is ``traefik`` (the only backend with an
    adapter today), an ``upstream`` is actually given — a route cannot
    be created on Traefik Manager with no backend target — and the request is
    not an ``adopt`` of a route Traefik Manager already has. A ``traefik``
    host created/edited with a blank ``upstream`` (e.g. a bare hostname
    reference filled in later, or via Resync) stays a local-only reference
    record until an ``upstream`` is set, same as before this feature
    existed. See ``docs/proxy_hosts.md``'s "Live-managed proxy hosts"
    section."""
    return body.router_provider is RouterProvider.TRAEFIK and bool(body.upstream) and not body.adopt


def _get_or_404(session: Session, proxy_host_id: int) -> ProxyHost:
    ph = session.get(ProxyHost, proxy_host_id)
    if ph is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"proxy host {proxy_host_id} not found")
    return ph


def _validate_service(session: Session, body: ProxyHostIn) -> Service:
    svc = session.get(Service, body.service_id)
    if svc is None:
        raise HTTPException(
            422,  # literal: the Starlette constant's name differs across versions
            f"service_id {body.service_id} does not reference a registered service",
        )
    return svc


@dataclass
class Prepared:
    """A validated, fully-resolved write: everything below is what will be
    written, with nothing left for Traefik Manager to default."""

    hostname: str
    entry_points: list[str]
    cert_resolver: str
    live_managed: bool
    catalog: ProxyCatalog | None
    route_options: dict[str, Any] = field(default_factory=dict)
    adopt_ref: str | None = None
    warnings: list[str] | None = None


def _find_route(routes: list[RouteInfo] | None, ref: str | None, hostname: str) -> RouteInfo | None:
    if not routes:
        return None
    return match_runtime_router(routes, route_ref=ref, hostname=hostname)


async def _conflict_message(
    adapters: AdapterBundle, hostname: str, route_name: str, own_ref: str | None
) -> str | None:
    """Refuse to write over a router that is not ours: Traefik Manager (or any
    other provider Traefik runs) already serves this hostname, or already has a
    route with the name we would create."""
    from orchestrator.adapters.proxy.traefik_manager import route_name_for_hostname

    candidates: list[RouteInfo] = []
    for routes in (await adapters.proxy.list_routes(), await adapters.proxy.list_runtime_routers()):
        candidates.extend(routes or [])
    for info in candidates:
        bare = bare_router_name(info.name)
        if own_ref and bare == own_ref:
            continue
        if hostname in info.hostnames or bare == route_name_for_hostname(hostname):
            return (
                f'Traefik already has a router "{info.name}" for {hostname}. Refusing to '
                "overwrite a route this orchestrator did not create - use “Resync from "
                "Traefik” to adopt it, or remove/rename it in Traefik Manager first."
            )
    return None


async def _prepare(
    session: Session,
    settings: Any,
    adapters: AdapterBundle,
    body: ProxyHostIn,
    *,
    existing: ProxyHost | None,
    live: LiveState | None = None,
) -> Prepared:
    """Validate + resolve a create/update BEFORE anything is written.

    Raises ``422`` (``409`` for a duplicate hostname) with
    ``detail = {"message", "errors": {field: message}}``.
    """
    errors: dict[str, str] = {}
    try:
        hostname = normalize_hostname(body.hostname)
    except ValueError as exc:
        hostname = body.hostname.strip().lower()
        errors["hostname"] = str(exc)

    clash = False
    if "hostname" not in errors:
        other = session.exec(select(ProxyHost).where(ProxyHost.hostname == hostname)).first()
        if other is not None and (existing is None or other.id != existing.id):
            errors["hostname"] = f"A proxy host for {hostname!r} already exists."
            clash = True

    live_managed = _is_live_managed(body)
    own_ref = (existing.extra or {}).get(TRAEFIK_MANAGER_ROUTE_REF_KEY) if existing else None
    catalog: ProxyCatalog | None = None
    entry_points: list[str] = list(body.entry_points or [])
    cert_resolver = body.cert_resolver
    adopt_ref: str | None = None

    if body.adopt and body.router_provider is RouterProvider.TRAEFIK:
        # Record only: take the entry points the route really has.
        live = live or await _live_state(settings, adapters)
        route = _find_route(live.routes, None, hostname) or _find_route(
            live.runtime, None, hostname
        )
        if route is not None:
            adopt_ref = bare_router_name(route.name)
            entry_points = entry_points or list(route.entry_points)
            if cert_resolver is None and route.cert_resolver:
                cert_resolver = route.cert_resolver

    if live_managed:
        catalog = await get_catalog(settings, adapters.proxy)
        if not entry_points:
            # Not chosen in this request: keep what is stored, else what the
            # route already has on Traefik Manager, else the default rule.
            if existing is not None and existing.entry_points:
                entry_points = list(existing.entry_points)
                if cert_resolver is None:
                    cert_resolver = existing.cert_resolver
            elif existing is not None:
                live = live or await _live_state(settings, adapters)
                route = _find_route(live.runtime, own_ref, hostname) or _find_route(
                    live.routes, own_ref, hostname
                )
                if route is not None and route.entry_points:
                    entry_points = list(route.entry_points)
                    if cert_resolver is None and route.cert_resolver:
                        cert_resolver = route.cert_resolver
            if not entry_points:
                entry_points = list(catalog.default_entrypoints)
        if cert_resolver is None:
            if existing is not None and existing.entry_points:
                cert_resolver = existing.cert_resolver
            else:
                all_plain = bool(entry_points) and all(
                    is_plain_http_entrypoint(
                        n, next((e.address for e in catalog.entrypoints or [] if e.name == n), "")
                    )
                    for n in entry_points
                )
                cert_resolver = "" if all_plain else catalog.default_cert_resolver
    cert_resolver = (cert_resolver or "").strip()

    # Route options: what the body says, else what is stored, else defaults.
    if body.route_options is not None:
        raw_options: dict[str, Any] = dict(body.route_options)
    else:
        raw_options = dict((existing.route_options if existing else None) or {})
    options = RouteOptions.from_dict(raw_options)
    # All-defaults is stored as {} so the YAML / DB copies of an untouched host
    # stay empty.
    route_options = {} if options == RouteOptions() else options.to_dict()

    # The hostname was checked above; a placeholder keeps its message out of
    # this second pass when it was already invalid.
    errors.update(
        validate_proxy_host(
            hostname=hostname if "hostname" not in errors else "placeholder.example",
            upstream=body.upstream,
            entry_points=entry_points,
            cert_resolver=cert_resolver,
            live_managed=live_managed,
            known_entry_points=catalog.entrypoints if catalog else None,
            known_cert_resolvers=catalog.cert_resolvers if catalog else None,
            route_options=raw_options,
        )
    )
    if live_managed and options.http_redirect and "http_redirect" not in errors:
        wanted = (settings.traefik_redirect_middleware or "redirect-to-https").strip()
        have = await adapters.proxy.list_middlewares()
        if have is not None and wanted not in {bare_router_name(m) for m in have}:
            errors["http_redirect"] = (
                f'The redirect middleware "{wanted}" does not exist in Traefik. Create it once '
                "(a redirectScheme middleware with scheme https, e.g. in your shared config) or set "
                "TRAEFIK_REDIRECT_MIDDLEWARE to the name you use."
            )

    if live_managed and "hostname" not in errors:
        from orchestrator.adapters.proxy.traefik_manager import route_name_for_hostname

        renamed = existing is not None and existing.hostname != hostname
        creating = existing is None or not own_ref
        if creating or renamed:
            msg = await _conflict_message(
                adapters, hostname, route_name_for_hostname(hostname), own_ref
            )
            if msg:
                errors["hostname"] = msg

    if errors:
        raise _fail(
            status.HTTP_409_CONFLICT if clash and len(errors) == 1 else 422,
            " · ".join(errors.values()),
            errors,
        )
    return Prepared(
        hostname=hostname,
        entry_points=entry_points,
        cert_resolver=cert_resolver,
        live_managed=live_managed,
        catalog=catalog,
        route_options=route_options,
        adopt_ref=adopt_ref,
    )


def _verification_error(verification: RouteVerification) -> dict[str, str]:
    field = "entry_points" if "entrypoint" in verification.message.lower() else "_"
    return {field: verification.message}


async def _verify_written(
    adapters: AdapterBundle,
    *,
    route_ref: str,
    hostname: str,
    entry_points: Sequence[str],
) -> RouteVerification:
    return await verify_route(
        adapters.proxy,
        route_ref=route_ref,
        hostname=hostname,
        expect_entry_points=entry_points,
    )


def _verification_warning(verification: RouteVerification) -> str | None:
    if verification.state == UNVERIFIED:
        return (
            "Saved, but could not be verified: Traefik's runtime routers could not be read, "
            "so it is unknown whether Traefik accepted the route."
        )
    if verification.state == NOT_LOADED:
        return (
            "Saved, but could not be verified: Traefik has not loaded the router yet. Check "
            "the Traefik status column in a moment (and that Traefik watches the file "
            "provider directory Traefik Manager writes to)."
        )
    return None


async def _rollback_created_route(
    adapters: AdapterBundle, route_ref: str, hostname: str, *, with_redirect: bool = False
) -> None:
    """Best effort: remove the route we just created so a rejected create
    leaves nothing broken behind on Traefik Manager."""
    try:
        await adapters.proxy.delete_route(route_ref, with_redirect=with_redirect)
    except ProxyRouteNotFound:
        pass
    except Exception as exc:
        log.warning(
            "proxy_host.rollback_delete_failed",
            route_ref=route_ref,
            hostname=hostname,
            error=str(exc),
        )


def _rejected_by_traefik(verification: RouteVerification, *, rolled_back: bool) -> HTTPException:
    detail = (
        "Traefik rejected the new route and it was removed again: "
        if rolled_back
        else "Traefik reports the router as broken: "
    )
    return _fail(
        422,
        detail + verification.message,
        _verification_error(verification),
        traefik_error=verification.message,
        rolled_back=rolled_back,
    )


@router.get("")
async def list_proxy_hosts(
    session: SessionDep, settings: SettingsDep, adapters: AdaptersDep
) -> list[dict[str, Any]]:
    services = _services_by_id(session)
    hosts = session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all()
    # One poll for the whole page load, not one per row.
    live = await _live_state(settings, adapters)
    return [_serialize(ph, services, live.hostnames, live) for ph in hosts]


@router.get("/{proxy_host_id}")
async def get_proxy_host(
    proxy_host_id: int, session: SessionDep, settings: SettingsDep, adapters: AdaptersDep
) -> dict[str, Any]:
    live = await _live_state(settings, adapters)
    return _serialize(
        _get_or_404(session, proxy_host_id), _services_by_id(session), live.hostnames, live
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_proxy_host(
    body: ProxyHostIn,
    session: SessionDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    svc = _validate_service(session, body)
    prep = await _prepare(session, settings, adapters, body, existing=None)
    warnings: list[str] = []
    ph = ProxyHost(
        hostname=prep.hostname,
        upstream=body.upstream,
        router_provider=body.router_provider,
        service_id=body.service_id,
        extra=dict(body.extra),
        wake_on_hit=body.wake_on_hit,
        entry_points=list(prep.entry_points),
        cert_resolver=prep.cert_resolver,
        route_options=dict(prep.route_options),
    )
    if prep.adopt_ref:
        ph.extra = {**ph.extra, TRAEFIK_MANAGER_ROUTE_REF_KEY: prep.adopt_ref}
    session.add(ph)
    session.flush()

    # Write-through: a live-managed (traefik) ProxyHost's create actually
    # creates the route on Traefik Manager. On failure, this request's
    # session is never committed (see web/deps.py's get_session — closing
    # an uncommitted session rolls it back), so no orphaned local-only row
    # is left behind pointing at nothing real. See docs/proxy_hosts.md.
    verification: RouteVerification | None = None
    if prep.live_managed:
        route_ref = await _write_through_create(
            adapters,
            hostname=ph.hostname,
            upstream=ph.upstream,
            entry_points=prep.entry_points,
            cert_resolver=prep.cert_resolver,
            options=RouteOptions.from_dict(prep.route_options),
        )
        # Did Traefik actually accept it? Traefik Manager happily writes a
        # router Traefik then refuses (unknown entry point, ...).
        verification = await _verify_written(
            adapters, route_ref=route_ref, hostname=ph.hostname, entry_points=prep.entry_points
        )
        if verification.state == ERROR:
            await _rollback_created_route(
                adapters,
                route_ref,
                ph.hostname,
                with_redirect=RouteOptions.from_dict(prep.route_options).http_redirect,
            )
            log.warning(
                "proxy_host.create_rejected_by_traefik",
                hostname=ph.hostname,
                error=verification.message,
            )
            raise _rejected_by_traefik(verification, rolled_back=True)
        warning = _verification_warning(verification)
        if warning:
            warnings.append(warning)
        ph.extra = {**ph.extra, TRAEFIK_MANAGER_ROUTE_REF_KEY: route_ref}
        session.add(ph)

    audit.record(
        session,
        actor=admin.username,
        action="proxy_host.created",
        target=f"proxy_host:{ph.hostname}",
        details={
            "id": ph.id,
            "upstream": ph.upstream,
            "router_provider": ph.router_provider.value,
            "service": svc.slug,
            "entry_points": list(ph.entry_points),
            "cert_resolver": ph.cert_resolver,
            "route_options": dict(ph.route_options or {}),
        },
    )
    probe_events = sync_traefik_probe(
        session, settings, hostname=ph.hostname, service_id=ph.service_id
    )
    _audit_probe_sync_events(session, admin, probe_events)
    session.commit()
    session.refresh(ph)
    maintenance_events = await _sync_maintenance_fallback(
        adapters, hostname=ph.hostname, service_slug=svc.slug
    )
    _audit_maintenance_sync_events(session, admin, maintenance_events)
    session.commit()
    live = await _live_state(settings, adapters)
    out = _serialize(ph, _services_by_id(session), live.hostnames, live)
    out["warnings"] = warnings
    return out


@router.post("/resync")
async def resync_proxy_hosts(
    session: SessionDep, admin: CurrentAdmin, settings: SettingsDep, adapters: AdaptersDep
) -> dict[str, Any]:
    """Pull Traefik's current router/backend state into the DB.

    This is the one place in this router that *writes* data sourced from
    Traefik, and it only ever runs when an admin clicks the button — never
    on a schedule or as a side effect of another request. See
    ``docs/proxy_hosts.md`` for exactly what it changes and why.

    For each ``ProxyHost`` whose hostname matches a live Traefik router,
    ``upstream`` is overwritten with the backend URL Traefik is actually
    routing to and ``last_seen_at`` is bumped — this intentionally
    overwrites a manually-set ``upstream``. ``router_provider`` and
    ``extra`` are never touched.

    A live hostname with no matching ``ProxyHost`` is never written to the
    DB (``service_id`` is required and Traefik has no concept of our
    ``Service`` model to supply one) — it is returned in ``discovered`` so
    the dashboard can offer it to the admin, who picks a service and submits
    it through the existing ``POST /api/proxy-hosts``.
    """
    routers = await _router_details_or_503(settings, adapters)
    # Same per-host verdict the list shows (and bust the entry-point cache: a
    # Resync is the "something changed on the gateway" button).
    await get_catalog(settings, adapters.proxy, refresh=True)
    runtime = await adapters.proxy.list_runtime_routers()
    live_for_status = LiveState(hostnames=None, routes=routers, runtime=runtime)

    # First router to claim a hostname wins ties (two routers rarely share a
    # Host() rule in practice; this just needs to be deterministic).
    hostname_to_router: dict[str, RouteInfo] = {}
    for info in routers:
        for hostname in info.hostnames:
            hostname_to_router.setdefault(hostname, info)

    hosts = session.exec(select(ProxyHost)).all()
    existing_hostnames = {ph.hostname for ph in hosts}

    now = datetime.now(UTC)
    updated: list[dict[str, Any]] = []
    probe_events: list[ProbeSyncEvent] = []
    for ph in hosts:
        # Backfill: every currently-linked ProxyHost gets its diagnostic
        # probe ensured here too, not just the ones Traefik currently
        # matches — this is what closes the gap for rows that already had a
        # service_id before this feature existed. A no-op when the probe
        # already matches, or when the host isn't linked to a service.
        probe_events.extend(
            sync_traefik_probe(session, settings, hostname=ph.hostname, service_id=ph.service_id)
        )
        match = hostname_to_router.get(ph.hostname)
        if match is None:
            continue
        upstream_changed = bool(match.upstream) and match.upstream != ph.upstream
        if upstream_changed:
            ph.upstream = match.upstream  # type: ignore[assignment]
        ph.last_seen_at = now
        session.add(ph)
        updated.append(
            {
                "id": ph.id,
                "hostname": ph.hostname,
                "upstream": ph.upstream,
                "router": match.name,
                "upstream_changed": upstream_changed,
                "traefik": _traefik_block(ph, live_for_status),
            }
        )

    discovered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for info in routers:
        for hostname in info.hostnames:
            if hostname in existing_hostnames or hostname in seen:
                continue
            seen.add(hostname)
            discovered.append(
                {
                    "hostname": hostname,
                    "upstream": info.upstream or "",
                    "router": info.name,
                    "entry_points": list(getattr(info, "entry_points", ())),
                }
            )
    discovered.sort(key=lambda d: d["hostname"])

    audit.record(
        session,
        actor=admin.username,
        action="proxy_host.resynced",
        target="proxy_host:*",
        details={
            "updated_count": len(updated),
            "discovered_count": len(discovered),
            "updated_hostnames": [u["hostname"] for u in updated],
            "discovered_hostnames": [d["hostname"] for d in discovered],
            "probes_synced_count": len(probe_events),
        },
    )
    _audit_probe_sync_events(session, admin, probe_events)
    session.commit()

    return {
        "updated": updated,
        "discovered": discovered,
        "updated_count": len(updated),
        "discovered_count": len(discovered),
    }


@router.put("/{proxy_host_id}")
async def update_proxy_host(
    proxy_host_id: int,
    body: ProxyHostIn,
    session: SessionDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    ph = _get_or_404(session, proxy_host_id)
    svc = _validate_service(session, body)
    prep = await _prepare(session, settings, adapters, body, existing=ph)
    old_svc = session.get(Service, ph.service_id) if ph.service_id else None

    before = {
        "hostname": ph.hostname,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
        "extra": ph.extra or {},
        "wake_on_hit": ph.wake_on_hit,
        "entry_points": list(ph.entry_points or []),
        "cert_resolver": ph.cert_resolver or "",
        "route_options": dict(ph.route_options or {}),
    }

    # Write-through: a live-managed (traefik) ProxyHost's update actually
    # updates the route on Traefik Manager BEFORE any local field changes,
    # so a failure here leaves both the DB row and the gateway exactly as
    # they were (request never commits) instead of disagreeing. Non-traefik
    # providers skip this entirely — reference-record-only, as before. See
    # docs/proxy_hosts.md's "Live-managed proxy hosts" section.
    new_extra = dict(body.extra)
    warnings: list[str] = []
    broken: RouteVerification | None = None
    if prep.live_managed:
        route_ref, created = await _write_through_update(
            adapters,
            ph,
            hostname=prep.hostname,
            upstream=body.upstream,
            entry_points=prep.entry_points,
            cert_resolver=prep.cert_resolver,
            options=RouteOptions.from_dict(prep.route_options),
        )
        new_extra[TRAEFIK_MANAGER_ROUTE_REF_KEY] = route_ref
        verification = await _verify_written(
            adapters, route_ref=route_ref, hostname=prep.hostname, entry_points=prep.entry_points
        )
        if verification.state == ERROR:
            if created:
                # We just made this route (no previous one to keep): take it back.
                await _rollback_created_route(
                    adapters,
                    route_ref,
                    prep.hostname,
                    with_redirect=RouteOptions.from_dict(prep.route_options).http_redirect,
                )
                raise _rejected_by_traefik(verification, rolled_back=True)
            # An edit: the new configuration IS on Traefik Manager now and we
            # cannot safely guess the previous one, so keep it, record it, and
            # report Traefik's verdict (raised after the commit below).
            broken = verification
        else:
            warning = _verification_warning(verification)
            if warning:
                warnings.append(warning)
    elif body.adopt and prep.adopt_ref:
        new_extra[TRAEFIK_MANAGER_ROUTE_REF_KEY] = prep.adopt_ref

    ph.hostname = prep.hostname
    ph.upstream = body.upstream
    ph.router_provider = body.router_provider
    ph.service_id = body.service_id
    ph.extra = new_extra
    ph.wake_on_hit = body.wake_on_hit
    if prep.live_managed or body.entry_points is not None:
        ph.entry_points = list(prep.entry_points)
        ph.cert_resolver = prep.cert_resolver
    if prep.live_managed or body.route_options is not None:
        ph.route_options = dict(prep.route_options)
    after = {
        "hostname": ph.hostname,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
        "extra": ph.extra,
        "wake_on_hit": ph.wake_on_hit,
        "entry_points": list(ph.entry_points or []),
        "cert_resolver": ph.cert_resolver or "",
        "route_options": dict(ph.route_options or {}),
    }
    changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}

    session.add(ph)
    audit.record(
        session,
        actor=admin.username,
        action="proxy_host.updated",
        target=f"proxy_host:{ph.hostname}",
        details={"id": ph.id, "service": svc.slug, "changes": changes},
    )
    probe_events = sync_traefik_probe(
        session,
        settings,
        hostname=ph.hostname,
        service_id=ph.service_id,
        old_hostname=before["hostname"],
        old_service_id=before["service_id"],
    )
    _audit_probe_sync_events(session, admin, probe_events)
    session.commit()
    session.refresh(ph)
    maintenance_events = await _sync_maintenance_fallback(
        adapters,
        hostname=ph.hostname,
        service_slug=svc.slug,
        old_hostname=before["hostname"],
        old_service_slug=old_svc.slug if old_svc else None,
    )
    _audit_maintenance_sync_events(session, admin, maintenance_events)
    session.commit()
    if broken is not None:
        log.warning(
            "proxy_host.update_rejected_by_traefik", hostname=ph.hostname, error=broken.message
        )
        raise _rejected_by_traefik(broken, rolled_back=False)
    live = await _live_state(settings, adapters)
    out = _serialize(ph, _services_by_id(session), live.hostnames, live)
    out["warnings"] = warnings
    return out


@router.delete("/{proxy_host_id}")
async def delete_proxy_host(
    proxy_host_id: int,
    session: SessionDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    ph = _get_or_404(session, proxy_host_id)
    hostname = ph.hostname
    old_svc = session.get(Service, ph.service_id) if ph.service_id else None
    details = {
        "id": ph.id,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
    }

    # Write-through: delete the real route first. A ProxyRouteNotFound is
    # tolerated (see _write_through_delete) — every other failure raises
    # and the local row is never touched. See docs/proxy_hosts.md.
    route_ref = (ph.extra or {}).get(TRAEFIK_MANAGER_ROUTE_REF_KEY)
    if ph.router_provider is RouterProvider.TRAEFIK and route_ref:
        await _write_through_delete(
            adapters,
            str(route_ref),
            with_redirect=RouteOptions.from_dict(ph.route_options).http_redirect,
        )

    probe_events = sync_traefik_probe(
        session,
        settings,
        hostname=None,
        service_id=None,
        old_hostname=ph.hostname,
        old_service_id=ph.service_id,
    )
    _audit_probe_sync_events(session, admin, probe_events)
    session.delete(ph)
    audit.record(
        session,
        actor=admin.username,
        action="proxy_host.deleted",
        target=f"proxy_host:{hostname}",
        details=details,
    )
    session.commit()
    maintenance_events = await _sync_maintenance_fallback(
        adapters,
        hostname=None,
        service_slug=None,
        old_hostname=hostname,
        old_service_slug=old_svc.slug if old_svc else None,
    )
    _audit_maintenance_sync_events(session, admin, maintenance_events)
    session.commit()
    return {"status": "deleted", "id": proxy_host_id, "hostname": hostname}


catalog_router = APIRouter(
    prefix="/api/proxy", tags=["proxy-hosts"], dependencies=[Depends(require_admin)]
)


@catalog_router.get("/entrypoints")
async def proxy_entrypoints(
    settings: SettingsDep, adapters: AdaptersDep, refresh: bool = False
) -> dict[str, Any]:
    """What the live proxy offers for a new route: its entry points (name +
    address), the known certificate resolvers, and the pre-selection for a new
    host. ``source`` is ``"unavailable"`` when Traefik Manager (or Traefik
    behind it) could not be read - the form then falls back to a free-text
    field. Cached ~30s; ``?refresh=true`` re-reads."""
    catalog = await get_catalog(settings, adapters.proxy, refresh=refresh)
    return catalog.as_dict()
