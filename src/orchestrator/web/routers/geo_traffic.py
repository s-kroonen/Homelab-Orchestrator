"""Read-only JSON hook for the GeoIP traffic-visibility feature.

One endpoint plus :func:`build_geo_view`, the aggregation it shares with the
``/traffic/geo`` dashboard page (``ui.py`` / ``templates/traffic_geo.html``).
See docs/geoip_traffic.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import Session, col, select

from orchestrator.db.models import GeoTrafficSample, ProxyHost, Service
from orchestrator.web.auth import require_admin
from orchestrator.web.deps import SessionDep, SettingsDep

router = APIRouter(
    prefix="/api/traffic", tags=["geo-traffic"], dependencies=[Depends(require_admin)]
)

#: How many of the most recent samples to aggregate over by default — a
#: handful of poll cycles' worth, enough for "what does recent traffic look
#: like" without the endpoint doing unbounded work.
DEFAULT_LIMIT = 500


@router.get("/geo")
async def get_geo_traffic(
    session: SessionDep,
    settings: SettingsDep,
    slug: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Recent aggregated country/IP counts. ``?slug=`` narrows to one
    service's proxy hosts; omitted, every proxy host's samples are summed.

    Shape::

        {
          "enabled": bool,
          "countries": [{"country_code", "country_name", "bucket", "hits"}],
          "top_ips": [{"client_ip", "bucket", "country_code", "hits"}],
          "sample_count": int,
        }

    Returns ``{"enabled": false, ...empty...}`` without touching
    ``GeoTrafficSample`` at all when ``GEOIP_ENABLED`` is false — same
    empty-state convention as ``build_traffic_view``.
    """
    return build_geo_view(session, enabled=settings.geoip_enabled, slug=slug, limit=limit)


def build_geo_view(
    session: Session, *, enabled: bool, slug: str | None = None, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """The aggregation behind both ``GET /api/traffic/geo`` and the
    ``/traffic/geo`` dashboard page (``ui.py``) — one implementation so the
    page and the JSON hook can never disagree. Same shape as the endpoint."""
    if not enabled:
        return {"enabled": False, "countries": [], "top_ips": [], "sample_count": 0}

    limit = max(1, min(limit, 5000))
    stmt = select(GeoTrafficSample).order_by(col(GeoTrafficSample.sampled_at).desc()).limit(limit)
    if slug is not None:
        proxy_host_ids = session.exec(
            select(ProxyHost.id)
            .join(Service, col(ProxyHost.service_id) == col(Service.id))
            .where(Service.slug == slug)
        ).all()
        stmt = stmt.where(col(GeoTrafficSample.proxy_host_id).in_(proxy_host_ids))
    rows = session.exec(stmt).all()

    by_country: dict[str, dict[str, Any]] = {}
    by_ip: dict[str, dict[str, Any]] = {}
    for row in rows:
        country_key = row.country_code or row.bucket
        country = by_country.setdefault(
            country_key,
            {
                "country_code": row.country_code,
                "country_name": row.country_name,
                "bucket": row.bucket,
                "hits": 0,
            },
        )
        country["hits"] += row.hit_count

        ip = by_ip.setdefault(
            row.client_ip,
            {
                "client_ip": row.client_ip,
                "bucket": row.bucket,
                "country_code": row.country_code,
                "hits": 0,
            },
        )
        ip["hits"] += row.hit_count

    countries = sorted(by_country.values(), key=lambda c: c["hits"], reverse=True)
    top_ips = sorted(by_ip.values(), key=lambda i: i["hits"], reverse=True)[:50]

    return {
        "enabled": True,
        "countries": countries,
        "top_ips": top_ips,
        "sample_count": len(rows),
    }


__all__ = ["DEFAULT_LIMIT", "build_geo_view", "router"]
