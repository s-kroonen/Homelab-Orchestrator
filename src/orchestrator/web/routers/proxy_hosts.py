"""Proxy-host CRUD — the DB-backed web equivalent of ``orchestrator-cli proxy``.

The database is the WORKING COPY. ``POST``/``PUT``/``DELETE`` here, the enable
switch and the bulk actions change the DB and nothing else: a ``ProxyHost``
whose ``router_provider`` is ``traefik`` and that has an upstream is
**live-managed**, and its route on Traefik Manager is written only by a push
(``orchestrator.registry.proxy_push``) - on "Save to YAML"
(``POST /api/registry/save``) or "Push now" (``POST /api/push``). Every
read-only check still happens at edit time (hostname, upstream, entry points,
resolver, route options, conflicts with routes that are not ours, the
redirect middleware), so a push rarely fails on something the form could have
said. Each host in the list carries a ``sync_state`` (``in_sync`` /
``pending_create`` / ``pending_update`` / ``detached`` / ``external`` /
``local``) with human ``sync_changes``. A host with provider
``pangolin``/``npm``/``other`` is a plain reference record: no adapter exists
for those backends yet. See ``docs/proxy_hosts.md`` "Pushing to Traefik
Manager". The ``live_status`` field (see ``orchestrator.registry.
traefik_status``) and ``POST /resync`` remain read-only/bulk-discovery.

``proxy_host`` is YAML-owned (``docs/architecture.md``), and the existing
``POST /api/registry/save`` / ``/api/registry/reset`` endpoints round-trip the
live DB to ``services.yaml``. The YAML schema nests proxy hosts under their
service, so ``service_id`` is required here too — matching
``orchestrator-cli proxy add <slug>``, which always takes one. Without this,
a proxy host with no service would silently never reach the saved YAML.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.proxy.base import ConfigFiles, RouteInfo, RouteOptions, find_route
from orchestrator.adapters.proxy.traefik_manager import is_plain_http_entrypoint
from orchestrator.adapters.proxy.verify import (
    UNVERIFIED,
    RouteVerification,
    bare_router_name,
    match_runtime_router,
    router_state,
)
from orchestrator.audit import log as audit
from orchestrator.db.models import ProxyHost, Service
from orchestrator.domain.enums import RouterProvider
from orchestrator.logging_setup import get_logger
from orchestrator.registry.archive import archive_proxy_host
from orchestrator.registry.config_files import normalize_config_file, unknown_file_problem
from orchestrator.registry.host_cascade import host_served
from orchestrator.registry.proxy_push import (
    SERVED_OVERLAY,
    TRAEFIK_MANAGER_ROUTE_REF_KEY,
    HostSync,
    add_pending_delete,
    config_file_view,
    conflict_message,
    desired_signature,
    evaluate,
    host_enabled,
    is_live_managed,
    move_to_configured_file,
    pending_deletes,
    ref_of,
    slug_of,
    take_pending_delete,
)
from orchestrator.registry.proxy_validation import (
    normalize_hostname,
    parse_entry_points,
    validate_cert_resolver,
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

#: ``live_status`` / ``traefik.state`` of a host that is switched off on
#: purpose (``ProxyHost.enabled`` False): never "missing" / "not loaded".
DISABLED = "disabled"


@dataclass
class LiveState:
    """One read of what the proxy has right now, shared by a whole response."""

    #: Hostnames some router covers; ``None`` = unknown (poll failed / unset).
    hostnames: set[str] | None
    #: Traefik Manager's own routes (``/api/routes``), when readable.
    routes: list[RouteInfo] | None
    #: Traefik's runtime routers with status/error, when readable.
    runtime: list[RouteInfo] | None
    #: Traefik Manager's config files, when readable (for the "lives in" note).
    config_files: ConfigFiles | None = None


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
    config_files = await adapters.proxy.list_config_files() if routes is not None else None
    return LiveState(hostnames=hostnames, routes=routes, runtime=runtime, config_files=config_files)


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


async def _service_tree_overlay(session: SessionDep) -> AsyncIterator[None]:
    """For the whole request, judge hosts against the service tree: a host whose
    service (or an ancestor) is disabled is not served, whatever its own switch says."""
    from orchestrator.registry.host_cascade import tree_map

    SERVED_OVERLAY.set(tree_map(session))
    try:
        yield
    finally:
        SERVED_OVERLAY.set(None)


router = APIRouter(
    prefix="/api/proxy-hosts",
    tags=["proxy-hosts"],
    dependencies=[Depends(require_admin), Depends(_service_tree_overlay)],
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


def tm_routes_for(routes: Sequence[RouteInfo] | None, hostname: str) -> list[RouteInfo]:
    """Every Traefik Manager route that serves ``hostname`` - the main router
    and, for example, a hand-written ``<name>-redirect`` on the plain-HTTP entry
    point."""
    host = hostname.strip().lower()
    return [r for r in routes or [] if host in {h.lower() for h in r.hostnames}]


def tm_state(routes: Sequence[RouteInfo] | None, hostname: str) -> str | None:
    """Traefik Manager's own on/off state for a host: ``enabled``, ``disabled``,
    ``partial`` (some of its routes on, some off) or ``None`` when TM is not
    readable or has no route for it."""
    if routes is None:
        return None
    found = tm_routes_for(routes, hostname)
    if not found:
        return None
    on = [bool(getattr(r, "enabled", True)) for r in found]
    if all(on):
        return "enabled"
    if not any(on):
        return "disabled"
    return "partial"


def _traefik_block(
    ph: ProxyHost, live: LiveState | None, expect: Sequence[str] | None = None
) -> dict[str, Any] | None:
    """Per-host Traefik status for list/get/resync: the runtime router's own
    status and error text (what Traefik Manager shows)."""
    if ph.router_provider is not RouterProvider.TRAEFIK:
        return None
    if not host_enabled(ph):
        # Switched off on purpose: Traefik is EXPECTED not to serve it, so its
        # absence from the runtime routers is not an error or "not loaded".
        return {
            "state": DISABLED,
            "message": "Disabled - the route is parked and Traefik is not serving it.",
            "router": None,
            "status": None,
            "entry_points": list(ph.entry_points or []),
            "cert_resolver": ph.cert_resolver or None,
        }
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
            if not getattr(route, "enabled", True):
                # Enabled here, switched off on Traefik Manager (by hand, or a
                # Reset to saved re-enabled the record): say so plainly.
                out["message"] = (
                    "Traefik Manager has this route disabled although it is enabled here - "
                    "use Enable to switch it back on."
                )
    return out


def _serialize(
    ph: ProxyHost,
    services: dict[int, Service],
    live_hostnames: set[str] | None,
    live: LiveState | None = None,
    sync: HostSync | None = None,
    files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    svc = services.get(ph.service_id)
    traefik = _traefik_block(ph, live)
    if traefik is not None and sync is not None and "disable" in sync.actions:
        # Switched off here, but the push that parks the route has not run yet.
        traefik["message"] = (
            "Disabled here - Traefik Manager still serves the route until you push."
        )
    return {
        # Where this host stands against Traefik Manager (registry/proxy_push.py):
        # in_sync / pending_create / pending_update / detached / external / local,
        # with human strings saying what differs.
        "sync_state": sync.state if sync is not None else None,
        "sync_changes": list(sync.changes) if sync is not None else [],
        "sync_pending": bool(sync is not None and sync.pending),
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
        "live_status": (
            DISABLED if not host_enabled(ph) else drift_status(ph.hostname, live_hostnames).value
        ),
        "enabled": ph.enabled,
        # Switched on AND its service (and every ancestor) is enabled; False with
        # enabled=True means "parked by its service", not by this host's own switch.
        "served": host_enabled(ph),
        # What Traefik Manager says about the same host (it is the source of
        # truth for routes): enabled / disabled / partial / None (unreadable or
        # no such route). A difference from `enabled` is shown as a mismatch.
        "tm_state": (
            tm_state(live.routes, ph.hostname)
            if live is not None and ph.router_provider is RouterProvider.TRAEFIK
            else None
        ),
        # Traefik's verdict on the router itself (state ok / error / not_loaded
        # / unverified, with Traefik's own error text when it is broken).
        "traefik": traefik,
        # Which Traefik Manager config file the route lives in vs where a new
        # route would go (informational): config_file (the host's own override),
        # config_file_resolved, tm_config_file, config_file_misplaced / _movable,
        # config_file_note.
        **(files if files is not None else _NO_FILES),
    }


_NO_FILES: dict[str, Any] = {
    "config_file": "",
    "config_file_resolved": "",
    "config_file_problem": None,
    "tm_config_file": None,
    "config_file_misplaced": False,
    "config_file_movable": False,
    "config_file_note": None,
}


def _files_view(
    settings: Any,
    adapters: AdapterBundle,
    ph: ProxyHost,
    services: dict[int, Service],
    live: LiveState,
    sync: HostSync,
) -> dict[str, Any]:
    if ph.router_provider is not RouterProvider.TRAEFIK:
        return dict(_NO_FILES)
    return config_file_view(
        adapters.proxy,
        settings,
        ph,
        slug_of(ph, services),
        sync.route,
        live.config_files,
    )


def _services_by_id(session: Session) -> dict[int, Service]:
    return {s.id: s for s in session.exec(select(Service)).all() if s.id is not None}


async def _sync_routes(
    settings: Any, adapters: AdapterBundle, live: LiveState
) -> list[RouteInfo] | None:
    """Traefik Manager's routes for sync classification: the read the page
    already made when a URL is configured, else the adapter's own (the dry-run
    adapter lists what it recorded; an unconfigured real one answers ``None``
    without touching the network)."""
    if settings.traefik_manager_url.strip():
        return live.routes
    return await adapters.proxy.list_routes()


def _sync_for(
    adapters: AdapterBundle,
    ph: ProxyHost,
    routes: list[RouteInfo] | None,
    services: dict[int, Service],
) -> HostSync:
    svc = services.get(ph.service_id) if ph.service_id else None
    return evaluate(adapters.proxy, ph, routes, svc.slug if svc else None)


async def _serialize_one(
    session: Session, settings: Any, adapters: AdapterBundle, ph: ProxyHost
) -> dict[str, Any]:
    services = _services_by_id(session)
    live = await _live_state(settings, adapters)
    routes = await _sync_routes(settings, adapters, live)
    sync = _sync_for(adapters, ph, routes, services)
    files = _files_view(settings, adapters, ph, services, live, sync)
    return _serialize(ph, services, live.hostnames, live, sync, files)


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
        if event.enabled is not None:
            details["enabled"] = event.enabled
        if event.config is not None:
            details["config"] = event.config
        audit.record(
            session,
            actor=admin.username,
            action=f"probe.{event.action}",
            target=f"service:{slug}",
            details=details,
        )


def _sync_probe_of(session: Session, settings: Any, ph: ProxyHost) -> list[ProbeSyncEvent]:
    """Ensure the auto-managed probe of ``ph`` matches it right now (URL, Host
    header and ``enabled``)."""
    return sync_traefik_probe(
        session,
        settings,
        hostname=ph.hostname,
        service_id=ph.service_id,
        enabled=host_served(session, ph),
    )


def _is_live_managed(body: ProxyHostIn) -> bool:
    """A ``ProxyHost`` is pushed to the real backend only when
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
    elif existing is None and catalog is not None and not body.adopt:
        # A NEW route is made the standard way (what "Format routes" rewrites old ones into):
        # Host header passed, HTTP -> HTTPS redirect router when Traefik has the middleware.
        from orchestrator.registry.route_standard import standard_options

        addresses = {e.name: e.address for e in catalog.entrypoints or []}
        serves_https = any(
            not is_plain_http_entrypoint(n, addresses.get(n, "")) for n in entry_points
        )
        raw_options = standard_options(
            None, redirect_available=catalog.redirect_available and serves_https
        ).to_dict()
    else:
        raw_options = dict((existing.route_options if existing else None) or {})
    options = RouteOptions.from_dict(raw_options)
    if options.config_file:
        try:
            raw_options["config_file"] = normalize_config_file(options.config_file)
        except ValueError as exc:
            errors["config_file"] = str(exc)
        else:
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
    if catalog is not None:
        # A resolver / file added to Traefik Manager a moment ago is not in the
        # 30 s cache yet: look again before telling the operator it does not exist.
        if "cert_resolver" in errors or (
            options.config_file
            and "config_file" not in errors
            and unknown_file_problem(options.config_file, catalog.config_files)
        ):
            catalog = await get_catalog(settings, adapters.proxy, refresh=True)
            if "cert_resolver" in errors:
                again = validate_cert_resolver(cert_resolver, catalog.cert_resolvers)
                if again is None:
                    del errors["cert_resolver"]
                else:
                    errors["cert_resolver"] = again
        if options.config_file and "config_file" not in errors:
            problem = unknown_file_problem(options.config_file, catalog.config_files)
            if problem:
                errors["config_file"] = problem
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
            if not own_ref:
                # Re-creating a host deleted here but not yet removed from
                # Traefik Manager reuses that route (it is not a conflict).
                tomb = next(
                    (t for t in pending_deletes(session) if t.get("hostname") == hostname), None
                )
                own_ref = tomb["route_ref"] if tomb else None
            msg = await conflict_message(
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


@router.get("")
async def list_proxy_hosts(
    session: SessionDep, settings: SettingsDep, adapters: AdaptersDep
) -> list[dict[str, Any]]:
    services = _services_by_id(session)
    hosts = session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all()
    # One poll for the whole page load, not one per row.
    live = await _live_state(settings, adapters)
    routes = await _sync_routes(settings, adapters, live)
    out: list[dict[str, Any]] = []
    for ph in hosts:
        sync = _sync_for(adapters, ph, routes, services)
        files = _files_view(settings, adapters, ph, services, live, sync)
        out.append(_serialize(ph, services, live.hostnames, live, sync, files))
    return out


@router.get("/{proxy_host_id}")
async def get_proxy_host(
    proxy_host_id: int, session: SessionDep, settings: SettingsDep, adapters: AdaptersDep
) -> dict[str, Any]:
    return await _serialize_one(session, settings, adapters, _get_or_404(session, proxy_host_id))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_proxy_host(
    body: ProxyHostIn,
    session: SessionDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    """Record a proxy host in the DB. Nothing is written to Traefik Manager
    here: a live-managed host becomes ``pending_create`` and is pushed on
    "Save to YAML" / "Push now" (``registry/proxy_push.py``). Everything that
    can be checked read-only IS checked now - hostname, upstream, entry
    points, resolver, route options, conflicts with routes that are not ours -
    so a push does not fail on something the form could have said."""
    svc = _validate_service(session, body)
    prep = await _prepare(session, settings, adapters, body, existing=None)
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
    elif prep.live_managed:
        # Re-creating a host that was deleted here but not yet removed from
        # Traefik Manager takes that route over; otherwise the reference the
        # push will create is known (and saved to YAML) up front.
        taken = take_pending_delete(session, prep.hostname)
        ref = taken["route_ref"] if taken else adapters.proxy.route_ref_for(prep.hostname)
        ph.extra = {**ph.extra, TRAEFIK_MANAGER_ROUTE_REF_KEY: ref}
    session.add(ph)
    session.flush()

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
        session,
        settings,
        hostname=ph.hostname,
        service_id=ph.service_id,
        enabled=host_served(session, ph),
    )
    _audit_probe_sync_events(session, admin, probe_events)
    session.commit()
    session.refresh(ph)
    out = await _serialize_one(session, settings, adapters, ph)
    out["warnings"] = []
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
    services_map = _services_by_id(session)
    updated: list[dict[str, Any]] = []
    probe_events: list[ProbeSyncEvent] = []
    for ph in hosts:
        # Backfill: every currently-linked ProxyHost gets its diagnostic
        # probe ensured here too, not just the ones Traefik currently
        # matches — this is what closes the gap for rows that already had a
        # service_id before this feature existed. A no-op when the probe
        # already matches, or when the host isn't linked to a service.
        match = hostname_to_router.get(ph.hostname)
        if match is None:
            probe_events.extend(_sync_probe_of(session, settings, ph))
            continue
        svc_slug = slug_of(ph, services_map)
        # Adopting Traefik Manager's state means "in sync" for a host whose
        # last push still matched: re-stamp it, or the adopted values would
        # read as local edits.
        was_current = bool(ph.pushed_sig) and ph.pushed_sig == desired_signature(ph, svc_slug)
        upstream_changed = bool(match.upstream) and match.upstream != ph.upstream
        if upstream_changed:
            ph.upstream = match.upstream  # type: ignore[assignment]
        # Traefik Manager is the source of truth for whether a route is on:
        # adopt its state (only when TM itself was read - a bare Traefik API has
        # no notion of a disabled route and would report everything enabled).
        enabled_changed = False
        if settings.traefik_manager_url.strip() and ph.router_provider is RouterProvider.TRAEFIK:
            state = tm_state(routers, ph.hostname)
            if state in ("enabled", "disabled") and ph.enabled != (state == "enabled"):
                ph.enabled = state == "enabled"
                enabled_changed = True
        ph.last_seen_at = now
        if was_current and (upstream_changed or enabled_changed):
            ph.pushed_sig = desired_signature(ph, svc_slug)
        session.add(ph)
        # After the enabled state was adopted, so the probe follows it.
        probe_events.extend(_sync_probe_of(session, settings, ph))
        updated.append(
            {
                "id": ph.id,
                "hostname": ph.hostname,
                "upstream": ph.upstream,
                "router": match.name,
                "upstream_changed": upstream_changed,
                "enabled_changed": enabled_changed,
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
    """Edit a proxy host in the DB only; the change is pushed to Traefik
    Manager on "Save to YAML" / "Push now" (a disabled host included - the
    push orders enable -> update -> disable so a parked route is safe)."""
    ph = _get_or_404(session, proxy_host_id)
    svc = _validate_service(session, body)
    prep = await _prepare(session, settings, adapters, body, existing=ph)

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

    new_extra = dict(body.extra)
    old_ref = ref_of(ph)
    if prep.live_managed:
        # The stored reference stays (a rename does not rename the route); a
        # host that has none yet gets the one the push will create.
        new_extra[TRAEFIK_MANAGER_ROUTE_REF_KEY] = old_ref or adapters.proxy.route_ref_for(
            prep.hostname
        )
    elif body.adopt and prep.adopt_ref:
        new_extra[TRAEFIK_MANAGER_ROUTE_REF_KEY] = prep.adopt_ref
    elif old_ref and is_live_managed(ph) and adapters.proxy.owns_route_ref(old_ref):
        # No longer live-managed (provider changed / upstream cleared): the
        # route this orchestrator made for it goes away on the next push.
        await _tombstone(session, adapters, ph)

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
        enabled=host_served(session, ph),
    )
    _audit_probe_sync_events(session, admin, probe_events)
    session.commit()
    session.refresh(ph)
    out = await _serialize_one(session, settings, adapters, ph)
    out["warnings"] = []
    return out


@router.delete("/{proxy_host_id}")
async def delete_proxy_host(
    proxy_host_id: int,
    session: SessionDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    return await delete_proxy_host_record(
        session, admin, settings, adapters, _get_or_404(session, proxy_host_id)
    )


async def _tombstone(session: Session, adapters: AdapterBundle, ph: ProxyHost) -> bool:
    """Remember that ``ph``'s Traefik Manager route has to be deleted on the
    next push. Only a route this orchestrator created is ever deleted (an
    adopted one is left alone), and only if Traefik Manager really has it -
    a host that was never pushed leaves nothing behind. The read is the only
    contact with Traefik Manager."""
    ref = ref_of(ph)
    if not ref or not adapters.proxy.owns_route_ref(ref):
        return False
    routes = await adapters.proxy.list_routes()
    if routes is not None and find_route(routes, ref, ph.hostname) is None:
        return False
    svc = session.get(Service, ph.service_id) if ph.service_id else None
    add_pending_delete(
        session,
        hostname=ph.hostname,
        route_ref=ref,
        with_redirect=RouteOptions.from_dict(ph.route_options).http_redirect,
        service_slug=svc.slug if svc else "",
    )
    return True


async def delete_proxy_host_record(
    session: Session,
    admin: CurrentAdmin,
    settings: Any,
    adapters: AdapterBundle,
    ph: ProxyHost,
) -> dict[str, Any]:
    """``DELETE /api/proxy-hosts/{id}``, callable (the bulk endpoint reuses it).
    DB only: a live-managed host's route is recorded as a pending deletion and
    removed from Traefik Manager on the next push."""
    proxy_host_id = ph.id
    hostname = ph.hostname
    details = {
        "id": ph.id,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
    }
    pending_delete = False
    if is_live_managed(ph):
        pending_delete = await _tombstone(session, adapters, ph)

    probe_events = sync_traefik_probe(
        session,
        settings,
        hostname=None,
        service_id=None,
        old_hostname=ph.hostname,
        old_service_id=ph.service_id,
    )
    _audit_probe_sync_events(session, admin, probe_events)
    archive_proxy_host(session, settings, ph, admin.username)
    session.delete(ph)
    audit.record(
        session,
        actor=admin.username,
        action="proxy_host.deleted",
        target=f"proxy_host:{hostname}",
        details={**details, "pending_route_delete": pending_delete},
    )
    session.commit()
    return {
        "status": "deleted",
        "id": proxy_host_id,
        "hostname": hostname,
        "pending_route_delete": pending_delete,
    }


class EnabledIn(BaseModel):
    enabled: bool


async def set_proxy_host_enabled(
    session: Session,
    admin: CurrentAdmin,
    settings: Any,
    adapters: AdapterBundle,
    ph: ProxyHost,
    enabled: bool,
) -> dict[str, Any]:
    """Switch a host on or off in the DB. Traefik Manager is not contacted:
    the new state is pending until the next push (``proxy_host.enabled`` /
    ``proxy_host.disabled`` are audited now; ``proxy_host.pushed`` when it is
    applied)."""
    was = ph.enabled
    ph.enabled = enabled
    session.add(ph)
    audit.record(
        session,
        actor=admin.username,
        action="proxy_host.enabled" if enabled else "proxy_host.disabled",
        target=f"proxy_host:{ph.hostname}",
        details={"id": ph.id, "changed": was != enabled},
    )
    # The host's diagnostic probe follows immediately (a local change, not a
    # push): a disabled host is not served, so its probe must not fail the
    # service. ``required`` / timeout are kept for the way back.
    probe_events = sync_traefik_probe(
        session,
        settings,
        hostname=ph.hostname,
        service_id=ph.service_id,
        enabled=host_served(session, ph),
    )
    _audit_probe_sync_events(session, admin, probe_events)
    session.commit()
    session.refresh(ph)
    return await _serialize_one(session, settings, adapters, ph)


async def set_proxy_host_wake_on_hit(
    session: Session, admin: CurrentAdmin, ph: ProxyHost, wake_on_hit: bool
) -> dict[str, Any]:
    """Flip only ``wake_on_hit`` - a local setting, nothing to write to the
    proxy, so it works on a disabled host too. Audited like the edit it is
    (``proxy_host.updated`` with the same ``changes`` shape as ``PUT``)."""
    before = ph.wake_on_hit
    ph.wake_on_hit = wake_on_hit
    session.add(ph)
    svc = session.get(Service, ph.service_id) if ph.service_id else None
    audit.record(
        session,
        actor=admin.username,
        action="proxy_host.updated",
        target=f"proxy_host:{ph.hostname}",
        details={
            "id": ph.id,
            "service": svc.slug if svc else None,
            "changes": (
                {"wake_on_hit": {"from": before, "to": wake_on_hit}}
                if before != wake_on_hit
                else {}
            ),
        },
    )
    session.commit()
    session.refresh(ph)
    return {"id": ph.id, "hostname": ph.hostname, "wake_on_hit": ph.wake_on_hit}


@router.post("/{proxy_host_id}/move-config-file")
async def move_config_file(
    proxy_host_id: int,
    session: SessionDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    """Opt-in: re-save this host's route into the config file it resolves to
    (its own ``config_file``, else ``TRAEFIK_MANAGER_CONFIG_FILE``) and verify the
    result. Traefik Manager IS written to here, immediately - this is an
    explicit action like Push now, not a local edit. Refused (409) for a route
    that is not ours, a disabled host, or when the move cannot be verified."""
    ph = _get_or_404(session, proxy_host_id)
    result = await move_to_configured_file(session, adapters, settings, ph, actor=admin.username)
    if not result.ok:
        session.rollback()
        raise _fail(status.HTTP_409_CONFLICT, result.message)
    session.commit()
    session.refresh(ph)
    out = await _serialize_one(session, settings, adapters, ph)
    out["move"] = result.as_dict()
    return out


@router.post("/{proxy_host_id}/enabled")
async def set_enabled(
    proxy_host_id: int,
    body: EnabledIn,
    session: SessionDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    """Enable or disable one proxy host - see :func:`set_proxy_host_enabled`."""
    ph = _get_or_404(session, proxy_host_id)
    return await set_proxy_host_enabled(session, admin, settings, adapters, ph, body.enabled)


catalog_router = APIRouter(
    prefix="/api/proxy", tags=["proxy-hosts"], dependencies=[Depends(require_admin)]
)


@catalog_router.get("/entrypoints")
async def proxy_entrypoints(
    settings: SettingsDep,
    adapters: AdaptersDep,
    refresh: bool = False,
    service: str | None = None,
    hostname: str = "example.com",
) -> dict[str, Any]:
    """What the live proxy offers for a new route: its entry points (name +
    address), the known certificate resolvers (``cert_resolvers`` plus
    ``cert_resolver_sources`` - where each was seen), Traefik Manager's config
    files (``config_files``, ``config_file_template`` and ``default_config_file``
    for the optional ``service`` slug / ``hostname``), and the pre-selection for a
    new host. ``source`` is ``"unavailable"`` when Traefik Manager (or Traefik
    behind it) could not be read - the form then falls back to a free-text
    field. Cached ~30s; ``?refresh=true`` re-reads."""
    catalog = await get_catalog(settings, adapters.proxy, refresh=refresh)
    slug = service
    if slug is not None and not slug.strip():
        slug = None
    return catalog.as_dict(service_slug=slug, hostname=hostname)
