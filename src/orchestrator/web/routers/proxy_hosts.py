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

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.proxy.base import ProxyRouteNotFound, RouteInfo, SyncEvent
from orchestrator.audit import log as audit
from orchestrator.db.models import ProxyHost, Service
from orchestrator.domain.enums import RouterProvider
from orchestrator.logging_setup import get_logger
from orchestrator.registry.traefik_probes import ProbeSyncEvent, sync_traefik_probe
from orchestrator.registry.traefik_status import (
    drift_status,
    fetch_router_details,
    fetch_router_hostnames,
)
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

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

# One DNS label: 1-63 chars, alnum at both ends, hyphens inside.
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
# Optional leading "*." for wildcard routes; at least one label. A single-label
# name ("nas") is allowed — internal split-horizon names are common here.
_HOSTNAME_RE = re.compile(rf"^(\*\.)?{_LABEL}(\.{_LABEL})*$")


def _normalize_hostname(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if not host:
        raise ValueError("hostname is required")
    if len(host) > 253 or not _HOSTNAME_RE.match(host):
        raise ValueError(
            f"{value!r} is not a valid hostname (expected e.g. app.example.com; "
            "no scheme, port or path)"
        )
    return host


class ProxyHostIn(BaseModel):
    """Create/replace payload. PUT takes the full record, like POST."""

    hostname: str
    upstream: str = ""
    router_provider: RouterProvider = RouterProvider.TRAEFIK
    service_id: int
    extra: dict[str, Any] = Field(default_factory=dict)
    #: See ProxyHost.wake_on_hit — per-route opt-in for the
    #: TrafficCollector-driven "wake on hit" stand-in. Defaults False.
    wake_on_hit: bool = False

    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str) -> str:
        return _normalize_hostname(v)

    @field_validator("upstream")
    @classmethod
    def _strip_upstream(cls, v: str) -> str:
        return v.strip()


def _serialize(
    ph: ProxyHost, services: dict[int, Service], live_hostnames: set[str] | None
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
        "last_seen_at": ph.last_seen_at.isoformat() if ph.last_seen_at else None,
        # Computed fresh on each request, never persisted — see
        # orchestrator.registry.traefik_status. "unknown" means Traefik polling
        # is not configured or the poll failed, NOT that the route is missing.
        "live_status": drift_status(ph.hostname, live_hostnames).value,
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


async def _write_through_create(adapters: AdapterBundle, *, hostname: str, upstream: str) -> str:
    """Create the real route on Traefik Manager for a new, live-managed
    (``router_provider == traefik``) ``ProxyHost``. Raises a ``502`` — the
    caller must not commit the local row when this raises (see
    ``docs/proxy_hosts.md``'s "Live-managed proxy hosts" section)."""
    try:
        return await adapters.proxy.create_route(hostname=hostname, upstream=upstream)
    except HTTPException:  # pragma: no cover - defensive, adapters don't raise this
        raise
    except Exception as exc:
        log.warning("proxy_host.live_route_create_failed", hostname=hostname, error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"could not create the route on Traefik Manager: {exc}",
        ) from exc


async def _write_through_update(
    adapters: AdapterBundle, ph: ProxyHost, *, hostname: str, upstream: str
) -> str:
    """Live-managed update write-through for ``PUT``.

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
            await adapters.proxy.update_route(existing_ref, hostname=hostname, upstream=upstream)
            return str(existing_ref)
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
    return await _write_through_create(adapters, hostname=hostname, upstream=upstream)


async def _write_through_delete(adapters: AdapterBundle, route_ref: str) -> None:
    """Delete the real route for a live-managed ``ProxyHost`` being deleted.

    A :class:`ProxyRouteNotFound` (the route is already gone on the
    backend) is logged and swallowed — the operator's intent ("this host
    should no longer exist") is still satisfiable from the local side.
    Every other failure raises a ``502`` and blocks the local delete."""
    try:
        await adapters.proxy.delete_route(route_ref)
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
    BOTH hold: ``router_provider`` is ``traefik`` (the only backend with an
    adapter today), AND an ``upstream`` is actually given — a route cannot
    be created on Traefik Manager with no backend target. A ``traefik``
    host created/edited with a blank ``upstream`` (e.g. a bare hostname
    reference filled in later, or via Resync) stays a local-only reference
    record until an ``upstream`` is set, same as before this feature
    existed. See ``docs/proxy_hosts.md``'s "Live-managed proxy hosts"
    section."""
    return body.router_provider is RouterProvider.TRAEFIK and bool(body.upstream)


def _get_or_404(session: Session, proxy_host_id: int) -> ProxyHost:
    ph = session.get(ProxyHost, proxy_host_id)
    if ph is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"proxy host {proxy_host_id} not found")
    return ph


def _validate_refs(
    session: Session, body: ProxyHostIn, *, exclude_id: int | None = None
) -> Service:
    svc = session.get(Service, body.service_id)
    if svc is None:
        raise HTTPException(
            422,  # literal: the Starlette constant's name differs across versions
            f"service_id {body.service_id} does not reference a registered service",
        )
    clash = session.exec(select(ProxyHost).where(ProxyHost.hostname == body.hostname)).first()
    if clash is not None and clash.id != exclude_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a proxy host for {body.hostname!r} already exists"
        )
    return svc


@router.get("")
async def list_proxy_hosts(
    session: SessionDep, settings: SettingsDep, adapters: AdaptersDep
) -> list[dict[str, Any]]:
    services = _services_by_id(session)
    hosts = session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all()
    # One poll for the whole page load, not one per row.
    live_hostnames = await _live_hostnames(settings, adapters)
    return [_serialize(ph, services, live_hostnames) for ph in hosts]


@router.get("/{proxy_host_id}")
async def get_proxy_host(
    proxy_host_id: int, session: SessionDep, settings: SettingsDep, adapters: AdaptersDep
) -> dict[str, Any]:
    live_hostnames = await _live_hostnames(settings, adapters)
    return _serialize(_get_or_404(session, proxy_host_id), _services_by_id(session), live_hostnames)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_proxy_host(
    body: ProxyHostIn,
    session: SessionDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    svc = _validate_refs(session, body)
    ph = ProxyHost(
        hostname=body.hostname,
        upstream=body.upstream,
        router_provider=body.router_provider,
        service_id=body.service_id,
        extra=dict(body.extra),
        wake_on_hit=body.wake_on_hit,
    )
    session.add(ph)
    session.flush()

    # Write-through: a live-managed (traefik) ProxyHost's create actually
    # creates the route on Traefik Manager. On failure, this request's
    # session is never committed (see web/deps.py's get_session — closing
    # an uncommitted session rolls it back), so no orphaned local-only row
    # is left behind pointing at nothing real. See docs/proxy_hosts.md.
    if _is_live_managed(body):
        route_ref = await _write_through_create(
            adapters, hostname=ph.hostname, upstream=ph.upstream
        )
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
    live_hostnames = await _live_hostnames(settings, adapters)
    return _serialize(ph, _services_by_id(session), live_hostnames)


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
    svc = _validate_refs(session, body, exclude_id=ph.id)
    old_svc = session.get(Service, ph.service_id) if ph.service_id else None

    before = {
        "hostname": ph.hostname,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
        "extra": ph.extra or {},
        "wake_on_hit": ph.wake_on_hit,
    }

    # Write-through: a live-managed (traefik) ProxyHost's update actually
    # updates the route on Traefik Manager BEFORE any local field changes,
    # so a failure here leaves both the DB row and the gateway exactly as
    # they were (request never commits) instead of disagreeing. Non-traefik
    # providers skip this entirely — reference-record-only, as before. See
    # docs/proxy_hosts.md's "Live-managed proxy hosts" section.
    new_extra = dict(body.extra)
    if _is_live_managed(body):
        route_ref = await _write_through_update(
            adapters, ph, hostname=body.hostname, upstream=body.upstream
        )
        new_extra[TRAEFIK_MANAGER_ROUTE_REF_KEY] = route_ref

    ph.hostname = body.hostname
    ph.upstream = body.upstream
    ph.router_provider = body.router_provider
    ph.service_id = body.service_id
    ph.extra = new_extra
    ph.wake_on_hit = body.wake_on_hit
    after = {
        "hostname": ph.hostname,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
        "extra": ph.extra,
        "wake_on_hit": ph.wake_on_hit,
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
    live_hostnames = await _live_hostnames(settings, adapters)
    return _serialize(ph, _services_by_id(session), live_hostnames)


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
        await _write_through_delete(adapters, str(route_ref))

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
