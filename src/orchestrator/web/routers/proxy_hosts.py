"""Proxy-host CRUD — the DB-backed web equivalent of ``orchestrator-cli proxy``.

``ProxyHost`` rows are *reference records*: a hostname -> upstream mapping the
operator maintains by hand so the orchestrator (and the operator) can see
which public route fronts which service. Nothing here talks to Traefik —
there is no live sync, discovery or traffic monitoring yet (see
``docs/proxy_hosts.md``).

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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from orchestrator.audit import log as audit
from orchestrator.db.models import ProxyHost, Service
from orchestrator.domain.enums import RouterProvider
from orchestrator.registry.traefik_status import drift_status, fetch_router_hostnames
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
    session.commit()
    session.refresh(ph)
    live_hostnames = await fetch_router_hostnames(settings)
    return _serialize(ph, _services_by_id(session), live_hostnames)


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
    session.commit()
    session.refresh(ph)
    live_hostnames = await fetch_router_hostnames(settings)
    return _serialize(ph, _services_by_id(session), live_hostnames)


@router.delete("/{proxy_host_id}")
async def delete_proxy_host(
    proxy_host_id: int, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    ph = _get_or_404(session, proxy_host_id)
    hostname = ph.hostname
    details = {
        "id": ph.id,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "service_id": ph.service_id,
    }
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
