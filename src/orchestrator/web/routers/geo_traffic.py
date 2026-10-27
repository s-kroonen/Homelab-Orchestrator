"""Read-only JSON hook for the GeoIP traffic-visibility feature.

One endpoint plus :func:`build_geo_view`, the aggregation it shares with the
countries panel on the ``/traffic`` page (``ui.py`` / ``templates/traffic.html``).
See docs/geoip_traffic.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import Session, col, select

from orchestrator.db.models import GeoTrafficSample, ProxyHost, Service
from orchestrator.web.auth import require_admin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.routers.traffic import DEFAULT_RANGE, RANGES, RangeKey

router = APIRouter(
    prefix="/api/traffic", tags=["geo-traffic"], dependencies=[Depends(require_admin)]
)

#: How many client IPs the "top IPs" list keeps.
TOP_IPS = 50


@router.get("/geo")
async def get_geo_traffic(
    session: SessionDep,
    settings: SettingsDep,
    slug: str | None = None,
    range: RangeKey = "24h",
) -> dict[str, Any]:
    """Aggregated country/IP counts over ``range`` (default 24h, filtered on
    ``sampled_at``). ``?slug=`` narrows to one service's proxy hosts.

    Shape::

        {
          "enabled": bool,
          "range": str,
          "countries": [{"country_code", "country_name", "bucket", "hits"}],
          "top_ips": [{"client_ip", "bucket", "country_code", "hits"}],
          "sample_count": int,
        }

    ``enabled`` false (and everything empty) when ``GEOIP_ENABLED`` is off.
    """
    slug = (slug or "").strip() or None
    return build_geo_view(session, enabled=settings.geoip_enabled, slug=slug, range_key=range)


def build_geo_view(
    session: Session,
    *,
    enabled: bool,
    slug: str | None = None,
    range_key: str = DEFAULT_RANGE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The aggregation behind both ``GET /api/traffic/geo`` and the
    countries on the ``/traffic`` page — one implementation so they never disagree.
    Aggregated in SQL so a 14-day range stays cheap."""
    if range_key not in RANGES:
        range_key = DEFAULT_RANGE
    if not enabled:
        return {
            "enabled": False,
            "range": range_key,
            "countries": [],
            "top_ips": [],
            "sample_count": 0,
        }

    since = (now or datetime.now(UTC)) - timedelta(seconds=RANGES[range_key])
    filters: list[Any] = [col(GeoTrafficSample.sampled_at) >= since]
    if slug is not None:
        proxy_host_ids = session.exec(
            select(ProxyHost.id)
            .join(Service, col(ProxyHost.service_id) == col(Service.id))
            .where(Service.slug == slug)
        ).all()
        filters.append(col(GeoTrafficSample.proxy_host_id).in_(proxy_host_ids))

    hits = func.sum(GeoTrafficSample.hit_count)
    country_rows = session.exec(
        select(
            GeoTrafficSample.country_code,
            func.max(GeoTrafficSample.country_name),
            GeoTrafficSample.bucket,
            hits,
        )
        .where(*filters)
        .group_by(col(GeoTrafficSample.country_code), col(GeoTrafficSample.bucket))
    ).all()
    # One row per country; local/unknown buckets carry an empty code and
    # are keyed by bucket instead (same as before).
    by_country: dict[str, dict[str, Any]] = {}
    for code, name, bucket, total in country_rows:
        key = code or bucket
        entry = by_country.setdefault(
            key,
            {"country_code": code, "country_name": name or "", "bucket": bucket, "hits": 0},
        )
        entry["hits"] += int(total or 0)
    countries = sorted(by_country.values(), key=lambda c: c["hits"], reverse=True)

    ip_rows = session.exec(
        select(
            GeoTrafficSample.client_ip,
            func.max(GeoTrafficSample.bucket),
            func.max(GeoTrafficSample.country_code),
            hits,
        )
        .where(*filters)
        .group_by(col(GeoTrafficSample.client_ip))
        .order_by(hits.desc())
        .limit(TOP_IPS)
    ).all()
    top_ips = [
        {"client_ip": ip, "bucket": bucket, "country_code": code or "", "hits": int(total or 0)}
        for ip, bucket, code, total in ip_rows
    ]

    sample_count = session.exec(
        select(func.count()).select_from(GeoTrafficSample).where(*filters)
    ).one()

    return {
        "enabled": True,
        "range": range_key,
        "countries": countries,
        "top_ips": top_ips,
        "sample_count": int(sample_count or 0),
    }


__all__ = ["TOP_IPS", "build_geo_view", "router"]
