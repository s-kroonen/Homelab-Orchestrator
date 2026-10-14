"""Proxy-host CRUD — the DB-backed web equivalent of ``orchestrator-cli proxy``.

``ProxyHost`` rows are *reference records*: a hostname -> upstream mapping the
operator maintains by hand so the orchestrator (and the operator) can see
which public route fronts which service. Most of this router never talks to
Traefik at all. The two exceptions are read-only: the ``live_status`` field
computed into every response (see ``orchestrator.registry.traefik_status``),
and ``POST /resync`` below, a manual, admin-triggered action that pulls
Traefik's current router/backend state into the DB — never automatic, never
scheduled (see ``docs/proxy_hosts.md``).

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

from orchestrator.audit import log as audit
from orchestrator.db.models import ProxyHost, Service
from orchestrator.domain.enums import RouterProvider
from orchestrator.registry.traefik_probes import ProbeSyncEvent, sync_traefik_probe
from orchestrator.registry.traefik_status import (
    RouterInfo,
    drift_status,
    fetch_router_details,
    fetch_router_hostnames,
)
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import SessionDep, SettingsDep

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
async def list_proxy_hosts(session: SessionDep, settings: SettingsDep) -> list[dict[str, Any]]:
    services = _services_by_id(session)
    hosts = session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all()
    # One poll for the whole page load, not one per row.
    live_hostnames = await fetch_router_hostnames(settings)
    return [_serialize(ph, services, live_hostnames) for ph in hosts]


@router.get("/{proxy_host_id}")
async def get_proxy_host(
    proxy_host_id: int, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    live_hostnames = await fetch_router_hostnames(settings)
    return _serialize(_get_or_404(session, proxy_host_id), _services_by_id(session), live_hostnames)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_proxy_host(
    body: ProxyHostIn, session: SessionDep, admin: CurrentAdmin, settings: SettingsDep
) -> dict[str, Any]:
    svc = _validate_refs(session, body)
    ph = ProxyHost(
        hostname=body.hostname,
        upstream=body.upstream,
        router_provider=body.router_provider,
        service_id=body.service_id,
        extra=body.extra,
    )
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
        },
    )
    probe_events = sync_traefik_probe(
        session, settings, hostname=ph.hostname, service_id=ph.service_id
    )
    _audit_probe_sync_events(session, admin, probe_events)
    session.commit()
    session.refresh(ph)
    live_hostnames = await fetch_router_hostnames(settings)
    return _serialize(ph, _services_by_id(session), live_hostnames)


@router.post("/resync")
async def resync_proxy_hosts(
    session: SessionDep, admin: CurrentAdmin, settings: SettingsDep
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
    routers = await fetch_router_details(settings)
    if routers is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Traefik is not configured (TRAEFIK_API_URL unset) or could not be "
            "reached — see docs/proxy_hosts.md#drift-detection-against-traefik-read-only.",
        )

    # First router to claim a hostname wins ties (two routers rarely share a
    # Host() rule in practice; this just needs to be deterministic).
    hostname_to_router: dict[str, RouterInfo] = {}
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
) -> dict[str, Any]:
    ph = _get_or_404(session, proxy_host_id)
    svc = _validate_refs(session, body, exclude_id=ph.id)

    before = {
        "hostname": ph.hostname,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
        "extra": ph.extra or {},
    }
    ph.hostname = body.hostname
    ph.upstream = body.upstream
    ph.router_provider = body.router_provider
    ph.service_id = body.service_id
    ph.extra = body.extra
    after = {
        "hostname": ph.hostname,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
        "extra": ph.extra,
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
    live_hostnames = await fetch_router_hostnames(settings)
    return _serialize(ph, _services_by_id(session), live_hostnames)


@router.delete("/{proxy_host_id}")
async def delete_proxy_host(
    proxy_host_id: int, session: SessionDep, admin: CurrentAdmin, settings: SettingsDep
) -> dict[str, Any]:
    ph = _get_or_404(session, proxy_host_id)
    hostname = ph.hostname
    details = {
        "id": ph.id,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
    }
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
    return {"status": "deleted", "id": proxy_host_id, "hostname": hostname}
