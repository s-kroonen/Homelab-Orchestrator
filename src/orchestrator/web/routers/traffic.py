"""View-model for the ``/traffic`` dashboard page.

Turns the raw, cumulative :class:`TrafficSample` rows (see
``orchestrator.scheduler.traffic_collector``) into the deltas a human reads:
a requests/min rate per proxy host, a sparkline of recent intervals, and a
2xx/3xx/4xx/5xx breakdown for the most recent interval. Nothing here talks to
Traefik directly — it only reads what the collector has already stored.

A rate needs two samples; a brand new poll (or a host with exactly one
sample) has none yet, and a counter decrease between two samples means
Traefik restarted — both read as "no rate available" (``None``), never as
zero or negative traffic.
"""

from __future__ import annotations

import itertools
from typing import Any

from sqlmodel import Session, col, desc, select

from orchestrator.db.models import ProxyHost, Service, TrafficSample

#: How many of the most recent samples feed the rate + sparkline for one host.
#: At the default 60s poll interval this is ~20 minutes of history — enough
#: to see a trend without the page doing unbounded work on a busy host.
WINDOW = 20

#: Sparkline geometry (a plain inline <svg>, no charting library).
_SPARK_W = 120
_SPARK_H = 28


def _code_bucket(code: str) -> str:
    if len(code) == 3 and code.isdigit():
        return f"{code[0]}xx"
    return "other"


def _rates_per_min(samples: list[TrafficSample]) -> list[float | None]:
    """One rate per consecutive pair of samples. ``None`` = counter reset
    (Traefik restarted) or non-increasing timestamps — never fabricated."""
    rates: list[float | None] = []
    for prev, cur in itertools.pairwise(samples):
        dt = (cur.sampled_at - prev.sampled_at).total_seconds()
        dr = cur.requests_total - prev.requests_total
        rates.append((dr / dt) * 60.0 if dt > 0 and dr >= 0 else None)
    return rates


def _code_breakdown(prev: TrafficSample, cur: TrafficSample) -> dict[str, int]:
    """Interval (delta) counts bucketed into 2xx/3xx/4xx/5xx/other, between
    two consecutive samples. A per-code decrease (restart) clamps to 0 rather
    than going negative."""
    out = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
    prev_codes: dict[str, int] = prev.by_code or {}
    cur_codes: dict[str, int] = cur.by_code or {}
    for code, cur_count in cur_codes.items():
        delta = cur_count - prev_codes.get(code, 0)
        out[_code_bucket(code)] += max(delta, 0)
    return out


def _sparkline_svg(values: list[float | None]) -> str | None:
    """A minimal inline ``<svg><polyline>`` sparkline. ``None`` points (a
    restart) are plotted as 0 — the gap itself isn't worth the complexity for
    a homelab-scale dashboard, and the host's own badge already flags it."""
    if not values:
        return None
    clean = [v if v is not None else 0.0 for v in values]
    peak = max(clean) or 1.0
    n = len(clean)
    step = _SPARK_W / max(n - 1, 1)
    points = " ".join(
        f"{i * step:.1f},{_SPARK_H - (v / peak) * (_SPARK_H - 2) - 1:.1f}"
        for i, v in enumerate(clean)
    )
    return (
        f'<svg viewBox="0 0 {_SPARK_W} {_SPARK_H}" width="{_SPARK_W}" height="{_SPARK_H}" '
        f'class="sparkline" role="img" aria-label="recent traffic trend">'
        f'<polyline points="{points}" fill="none" stroke="#5b8def" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )


def _host_row(ph: ProxyHost, svc: Service | None, samples: list[TrafficSample]) -> dict[str, Any]:
    rates = _rates_per_min(samples)
    last_rate = rates[-1] if rates else None
    code_breakdown = _code_breakdown(samples[-2], samples[-1]) if len(samples) >= 2 else None
    return {
        "id": ph.id,
        "hostname": ph.hostname,
        "service_slug": svc.slug if svc else None,
        "service_name": svc.name if svc else None,
        "service_group": (svc.group if svc else "") or "",
        "sample_count": len(samples),
        "rate_per_min": round(last_rate, 1) if last_rate is not None else None,
        "restarted": len(samples) >= 2 and last_rate is None,
        "sparkline": _sparkline_svg(rates),
        "code_breakdown": code_breakdown,
        "last_sampled_at": samples[-1].sampled_at.isoformat() if samples else None,
    }


def _group_hosts(host_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket host rows by their service's ``group`` — the same convention
    ``ui._group_rows`` uses on the main dashboard. A host with no linked
    service, or whose service is ungrouped, lands in "Ungrouped"."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in host_rows:
        buckets.setdefault(row["service_group"], []).append(row)
    named = sorted(name for name in buckets if name)
    ordered = named + ([""] if "" in buckets else [])
    return [{"name": name or "Ungrouped", "hosts": buckets[name]} for name in ordered]


def build_traffic_view(session: Session, *, traefik_enabled: bool) -> dict[str, Any]:
    """Everything the ``/traffic`` template needs. ``traefik_enabled=False``
    short-circuits without touching the DB — there is nothing to show."""
    if not traefik_enabled:
        return {
            "enabled": False,
            "has_samples": False,
            "overall": None,
            "groups": [],
            "host_count": 0,
        }

    services_by_id = {s.id: s for s in session.exec(select(Service)).all() if s.id is not None}
    hosts = session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all()

    host_rows: list[dict[str, Any]] = []
    any_samples = False
    overall_rate = 0.0
    overall_known = False

    for ph in hosts:
        samples = list(
            reversed(
                session.exec(
                    select(TrafficSample)
                    .where(col(TrafficSample.proxy_host_id) == ph.id)
                    .order_by(desc(col(TrafficSample.sampled_at)))
                    .limit(WINDOW)
                ).all()
            )
        )
        if samples:
            any_samples = True
        svc = services_by_id.get(ph.service_id) if ph.service_id is not None else None
        row = _host_row(ph, svc, samples)
        host_rows.append(row)
        if row["rate_per_min"] is not None:
            overall_rate += row["rate_per_min"]
            overall_known = True

    overall = {
        "requests_per_min": round(overall_rate, 1) if overall_known else None,
        "hosts_with_rate": sum(1 for r in host_rows if r["rate_per_min"] is not None),
        "total_hosts": len(host_rows),
    }

    return {
        "enabled": True,
        "has_samples": any_samples,
        "overall": overall,
        "groups": _group_hosts(host_rows),
        "host_count": len(hosts),
    }


__all__ = ["WINDOW", "build_traffic_view"]
