"""JSON for the ``/traffic`` dashboard: time series rolled up from Traefik's
access log, plus the access-log collector's status.

Everything here only reads what
:class:`~orchestrator.scheduler.access_log_collector.AccessLogCollector`
already stored (:class:`TrafficRollup` rows, the ``access_log.status``
setting) — nothing talks to Traefik or Traefik Manager directly.

* ``GET /api/traffic/series?range=1h&slug=`` — zero-filled buckets over the
  chosen range, overall status-class counts and latency, and per-host totals
  with a hits sparkline series. See :func:`build_series`.
* ``GET /api/traffic/collector-status`` — the last poll's summary plus a
  ready-to-render ``view`` (level / headline / hint) shared with the
  server-rendered status strip on both traffic pages.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends
from sqlmodel import Session, col, select

from orchestrator.config import Settings
from orchestrator.db.models import ProxyHost, Service, ServiceState, TrafficRollup
from orchestrator.scheduler.access_log_collector import access_log_configured, load_status
from orchestrator.web.auth import require_admin
from orchestrator.web.deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/traffic", tags=["traffic"], dependencies=[Depends(require_admin)])

RangeKey = Literal["15m", "1h", "6h", "24h", "7d", "14d"]

#: Range key -> seconds. Shared with the geo view's ``range`` filter.
RANGES: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "14d": 14 * 86400,
}
DEFAULT_RANGE = "24h"


def step_for(range_s: int) -> int:
    """Bucket width: <=1h -> 1 min, 6h -> 5 min, 24h -> 15 min, longer -> 1 h."""
    if range_s <= 3600:
        return 60
    if range_s <= 6 * 3600:
        return 300
    if range_s <= 24 * 3600:
        return 900
    return 3600


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def range_window(range_key: str, now: datetime | None = None) -> tuple[datetime, datetime, int]:
    """``(start, end, step_s)`` as aware UTC, end-exclusive, aligned to the
    step so the current (partial) bucket is the last one."""
    range_s = RANGES[range_key]
    step = step_for(range_s)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    end_ts = (int(now.timestamp()) // step + 1) * step
    start_ts = end_ts - range_s
    start = datetime.fromtimestamp(start_ts, UTC)
    end = datetime.fromtimestamp(end_ts, UTC)
    return start, end, step


def _avg(total: float, count: int) -> float | None:
    return round(total / count, 1) if count else None


def build_series(
    session: Session,
    *,
    range_key: str = DEFAULT_RANGE,
    slug: str | None = None,
    enabled: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Zero-filled time series over ``range_key``.

    ``overall`` honours ``slug`` (only that service's proxy hosts);
    ``hosts`` always lists every proxy host so a table built from it can
    switch between services without a second request.
    """
    if range_key not in RANGES:
        range_key = DEFAULT_RANGE
    start, end, step = range_window(range_key, now)
    n = RANGES[range_key] // step

    services = {s.id: s for s in session.exec(select(Service)).all() if s.id is not None}
    # Each service's last verdict, so a host whose guest is stopped (DOWN) is
    # read in that light: its failing requests are explained, not a mystery.
    states = {st.service_id: st for st in session.exec(select(ServiceState)).all()}
    hosts = [h for h in session.exec(select(ProxyHost)).all() if h.id is not None]
    in_scope = {
        h.id
        for h in hosts
        if slug is None
        or (h.service_id is not None and getattr(services.get(h.service_id), "slug", None) == slug)
    }

    overall: dict[str, list[Any]] = {k: [0] * n for k in ("hits", "s2xx", "s3xx", "s4xx", "s5xx")}
    dsum = [0.0] * n
    dcount = [0] * n
    dmax: list[float | None] = [None] * n

    per_host: dict[int, dict[str, Any]] = {
        h.id: {
            "hits": [0] * n,
            "s5xx": [0] * n,
            "t_hits": 0,
            "t_s4xx": 0,
            "t_s5xx": 0,
            "dsum": 0.0,
            "dcount": 0,
            "dmax": None,
        }
        for h in hosts
        if h.id is not None
    }

    rows = session.exec(
        select(TrafficRollup).where(
            col(TrafficRollup.bucket_start) >= start, col(TrafficRollup.bucket_start) < end
        )
    ).all()
    for r in rows:
        bs = r.bucket_start
        bs = bs.replace(tzinfo=UTC) if bs.tzinfo is None else bs.astimezone(UTC)
        i = int((bs - start).total_seconds()) // step
        if not 0 <= i < n:
            continue
        ph = per_host.get(r.proxy_host_id)
        if ph is not None:
            ph["hits"][i] += r.hits
            ph["s5xx"][i] += r.s5xx
            ph["t_hits"] += r.hits
            ph["t_s4xx"] += r.s4xx
            ph["t_s5xx"] += r.s5xx
            ph["dsum"] += r.duration_ms_sum
            ph["dcount"] += r.duration_ms_count
            if r.duration_ms_max is not None and (
                ph["dmax"] is None or r.duration_ms_max > ph["dmax"]
            ):
                ph["dmax"] = r.duration_ms_max
        if r.proxy_host_id not in in_scope:
            continue
        overall["hits"][i] += r.hits
        overall["s2xx"][i] += r.s2xx
        overall["s3xx"][i] += r.s3xx
        overall["s4xx"][i] += r.s4xx
        overall["s5xx"][i] += r.s5xx
        dsum[i] += r.duration_ms_sum
        dcount[i] += r.duration_ms_count
        if r.duration_ms_max is not None:
            current = dmax[i]
            dmax[i] = r.duration_ms_max if current is None else max(current, r.duration_ms_max)

    overall["avg_ms"] = [_avg(dsum[i], dcount[i]) for i in range(n)]
    overall["max_ms"] = [round(v, 1) if v is not None else None for v in dmax]
    known_max = [v for v in dmax if v is not None]
    totals = {
        "hits": sum(overall["hits"]),
        "s2xx": sum(overall["s2xx"]),
        "s3xx": sum(overall["s3xx"]),
        "s4xx": sum(overall["s4xx"]),
        "s5xx": sum(overall["s5xx"]),
        "avg_ms": _avg(sum(dsum), sum(dcount)),
        "max_ms": round(max(known_max), 1) if known_max else None,
    }

    host_rows: list[dict[str, Any]] = []
    for h in hosts:
        assert h.id is not None
        svc = services.get(h.service_id) if h.service_id is not None else None
        ph = per_host[h.id]
        st = states.get(svc.id) if svc is not None else None
        host_rows.append(
            {
                "proxy_host_id": h.id,
                "hostname": h.hostname,
                "service_slug": svc.slug if svc else None,
                "service_name": svc.name if svc else None,
                # The service's last health verdict (healthy/failed/unknown/
                # down); None for an unlinked host or a never-scanned service.
                "service_state": st.last_verdict.value if st is not None else None,
                "service_state_reason": (st.notes or "") if st is not None else None,
                "totals": {
                    "hits": ph["t_hits"],
                    "s4xx": ph["t_s4xx"],
                    "s5xx": ph["t_s5xx"],
                    "avg_ms": _avg(ph["dsum"], ph["dcount"]),
                    "max_ms": round(ph["dmax"], 1) if ph["dmax"] is not None else None,
                },
                "hits": ph["hits"],
                "s5xx": ph["s5xx"],
            }
        )
    host_rows.sort(key=lambda r: (-r["totals"]["hits"], r["hostname"]))

    return {
        "enabled": enabled,
        "range": range_key,
        "slug": slug,
        "step_s": step,
        "from": _iso(start),
        "to": _iso(end),
        "buckets": [_iso(start + timedelta(seconds=i * step)) for i in range(n)],
        "overall": overall,
        # Range totals for ``overall`` (extra to the per-bucket arrays) so a
        # stat tile never re-derives a weighted average from bucket averages.
        "totals": totals,
        "hosts": host_rows,
    }


# -- collector status ----------------------------------------------------------

_SETUP_HINT = (
    "In Traefik Manager, point ACCESS_LOG_PATH (or Settings → access log path) at "
    "Traefik's access log, and make sure Traefik has accessLog enabled "
    "(format: json recommended)."
)


def _age_s(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (now - ts).total_seconds())


def status_view(
    status: dict[str, Any] | None, *, settings: Settings, now: datetime | None = None
) -> dict[str, Any]:
    """Turn the persisted status into what the strip renders.

    ``level``: ``ok`` (compact strip), ``warn``/``error`` (actionable
    notice), ``idle`` (configured, no poll yet), ``off`` (not configured).
    """
    now = now or datetime.now(UTC)
    configured = access_log_configured(settings)
    interval = settings.access_log_poll_interval_s
    base: dict[str, Any] = {
        "configured": configured,
        "poll_interval_s": interval,
        "last_poll_at": None,
        "age_s": None,
        "lines": 0,
        "parsed": 0,
        "matched": 0,
        "new_entries": 0,
        "unmatched": [],
        "error": None,
        "hint": None,
        "headline": "",
    }
    if status is None:
        if not configured:
            return {
                **base,
                "level": "off",
                "headline": "Access-log collection is not running.",
                "hint": (
                    "It needs PROXY_ADAPTER=traefik_manager with TRAEFIK_MANAGER_URL and "
                    "TRAEFIK_MANAGER_API_KEY set, DRY_RUN=false and ACCESS_LOG_ENABLED=true. "
                    "TRAEFIK_API_URL is not required."
                ),
            }
        return {
            **base,
            "level": "idle",
            "headline": f"Waiting for the first access-log poll (every {interval}s).",
        }

    view = {
        **base,
        "last_poll_at": status.get("last_poll_at"),
        "age_s": _age_s(status.get("last_poll_at"), now),
        "lines": int(status.get("lines_read") or 0),
        "parsed": int(status.get("parsed") or 0),
        "matched": int(status.get("matched") or 0),
        "new_entries": int(status.get("new_entries") or 0),
        "unmatched": list(status.get("unmatched_routers") or []),
        "error": status.get("error"),
    }
    if not status.get("ok", True):
        return {
            **view,
            "level": "error",
            "headline": "Traefik Manager could not provide the access log.",
            "hint": _SETUP_HINT,
        }
    if view["lines"] == 0:
        return {
            **view,
            "level": "warn",
            "headline": "The access log came back empty.",
            "hint": _SETUP_HINT,
        }
    if view["parsed"] == 0:
        return {
            **view,
            "level": "warn",
            "headline": f"None of the {view['lines']} access-log lines could be parsed.",
            "hint": "Set Traefik's accessLog.format to json (or the default common format).",
        }
    if view["matched"] == 0:
        return {
            **view,
            "level": "warn",
            "headline": f"None of the {view['parsed']} access-log lines matched a proxy host.",
            "hint": (
                "Each router listed above needs a proxy host in the orchestrator for its traffic "
                "to count: open Proxy hosts and press Resync."
            ),
        }
    stale = view["age_s"] is not None and view["age_s"] > max(3 * interval, 120)
    return {
        **view,
        "level": "warn" if stale else "ok",
        "headline": "The collector has not polled recently." if stale else "",
        "hint": "Check the orchestrator logs for access_log_collector." if stale else None,
    }


def collector_status_payload(session: Session, settings: Settings) -> dict[str, Any]:
    status = load_status(session)
    return {"status": status, "view": status_view(status, settings=settings)}


@router.get("/series")
async def get_series(
    session: SessionDep,
    settings: SettingsDep,
    range: RangeKey = "24h",
    slug: str | None = None,
) -> dict[str, Any]:
    slug = (slug or "").strip() or None
    return build_series(
        session, range_key=range, slug=slug, enabled=access_log_configured(settings)
    )


@router.get("/collector-status")
async def get_collector_status(session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    return collector_status_payload(session, settings)


__all__ = [
    "DEFAULT_RANGE",
    "RANGES",
    "build_series",
    "collector_status_payload",
    "range_window",
    "router",
    "status_view",
    "step_for",
]
