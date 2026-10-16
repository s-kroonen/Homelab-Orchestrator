"""Drift detection and read-only discovery against a live Traefik instance.

This is deliberately NOT an :mod:`orchestrator.adapters` adapter: there is
nothing to dry-run about reading Traefik's current router list, no lifecycle
(``start``/``stop``) worth managing, and no mutation path at all — the
orchestrator must never attempt to write to Traefik (see ``docs/proxy_hosts.md``).
Everything here is a short-lived GET against Traefik's own API, used either
to flag ``ProxyHost`` records that have no matching live Traefik router
(:func:`fetch_router_hostnames`, :func:`drift_status`) or to resolve full
router/backend detail for the manual "Resync from Traefik" action
(:func:`fetch_router_details`), which lives in
``orchestrator.web.routers.proxy_hosts``.

Enable with ``TRAEFIK_API_URL`` (Traefik started with ``--api=true``). Left
unset, :func:`fetch_router_hostnames` is never called with a usable URL and
every host reads as :class:`DriftStatus.UNKNOWN` — this feature is fully
optional, the same way the PBS/Proxmox adapters degrade when unconfigured.

Failures (DNS, connect, TLS, timeout, non-2xx, bad JSON) are caught here and
turned into the ``UNKNOWN`` sentinel rather than raised — a Traefik outage or
misconfiguration must never crash a dashboard page load or an unrelated
request.

:func:`fetch_router_metrics` adds one more read-only GET, against
``{TRAEFIK_API_URL}/metrics`` (Traefik started with
``--metrics.prometheus=true``). Unlike every other endpoint here, that one
answers in Prometheus's plain-text exposition format, not JSON — hence
:func:`parse_router_metrics`, a small forgiving line parser rather than a
``prometheus_client`` dependency (that package is for *exposing* metrics,
not reading someone else's). It only understands the three metric families
the traffic dashboard needs; every other family Traefik exposes is ignored.
A single poll is just "requests since Traefik's process started" — turning
that into a trend is ``orchestrator.scheduler.traffic_collector``'s job, by
polling repeatedly and storing what it sees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

# Matches each Host(`...`) / HostSNI(`...`) clause inside a router rule.
# Traefik rules OR multiple hosts together, e.g.:
#   Host(`a.example.com`) || Host(`b.example.com`)
_HOST_CLAUSE_RE = re.compile(r"Host(?:SNI)?\(\s*((?:`[^`]*`\s*,?\s*)+)\)")
_QUOTED_RE = re.compile(r"`([^`]*)`")


class DriftStatus(StrEnum):
    """A ``ProxyHost``'s status against Traefik's live router list."""

    MATCHED = "matched"  # a live router's Host() rule covers this hostname
    MISSING = "missing"  # Traefik reachable, but no router covers it
    UNKNOWN = "unknown"  # not configured, or the poll failed — not the same as MISSING


def extract_hostnames(rule: str) -> set[str]:
    """Pull every hostname out of a router ``rule`` string's Host()/HostSNI() clauses.

    Handles multiple clauses OR'd together (``Host(`a`) || Host(`b`)``) and
    multiple comma-separated hosts in one clause (``Host(`a`, `b`)``).
    Non-Host predicates (``PathPrefix``, ``Headers``, ...) are ignored.
    """
    hosts: set[str] = set()
    for clause in _HOST_CLAUSE_RE.findall(rule):
        hosts.update(h.strip().lower().rstrip(".") for h in _QUOTED_RE.findall(clause))
    return hosts


def _traefik_auth(settings: Settings) -> tuple[str, str] | None:
    if settings.traefik_api_username and settings.traefik_api_password.get_secret_value():
        return (settings.traefik_api_username, settings.traefik_api_password.get_secret_value())
    return None


def _traefik_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=settings.traefik_api_verify_tls,
        timeout=settings.traefik_api_timeout_s,
        auth=_traefik_auth(settings),
    )


async def _traefik_get(client: httpx.AsyncClient, url: str) -> Any | None:
    """GET one Traefik API endpoint, returning parsed JSON or ``None``.

    Every failure mode (timeout, connect error, non-2xx, non-JSON body) is
    caught and logged here, never raised — shared by every function in this
    module so a Traefik hiccup degrades the same way everywhere.
    """
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException as exc:
        log.warning("traefik.poll_timeout", url=url, error=str(exc))
        return None
    except httpx.HTTPError as exc:
        log.warning("traefik.poll_failed", url=url, error=str(exc))
        return None
    except ValueError as exc:  # non-JSON body
        log.warning("traefik.poll_bad_response", url=url, error=str(exc))
        return None


async def fetch_router_hostnames(settings: Settings) -> set[str] | None:
    """Return every hostname Traefik currently has an HTTP router for.

    Returns ``None`` (never raises) when drift checking is disabled
    (``TRAEFIK_API_URL`` unset) or the poll could not be completed.
    """
    base_url = settings.traefik_api_url.strip()
    if not base_url:
        return None

    url = base_url.rstrip("/") + "/api/http/routers"
    async with _traefik_client(settings) as client:
        routers = await _traefik_get(client, url)
    if routers is None:
        return None
    if not isinstance(routers, list):
        log.warning("traefik.poll_bad_response", url=url, error="expected a JSON array")
        return None

    hostnames: set[str] = set()
    for router in routers:
        if not isinstance(router, dict):
            continue
        rule = router.get("rule")
        if isinstance(rule, str):
            hostnames.update(extract_hostnames(rule))
    return hostnames


@dataclass(frozen=True)
class RouterInfo:
    """One Traefik HTTP router, with its hostnames resolved to a backend URL.

    ``upstream`` is ``None`` when the router's Traefik *service* (unrelated
    to our own ``Service`` model — same name, different thing) could not be
    resolved to a ``loadBalancer`` backend: the ``/api/http/services`` call
    failed, the service wasn't found, it uses a provider this module doesn't
    understand (e.g. ``weighted``/``mirroring``), or it has no servers.
    """

    name: str
    hostnames: set[str]
    upstream: str | None


def _resolve_service_upstream(service_obj: Any) -> str | None:
    """Pull a backend URL out of one ``/api/http/services`` entry.

    Only the common ``loadBalancer`` shape is understood. A service with
    multiple servers (load-balanced) has its server URLs joined with a
    comma — this is a cosmetic display value for the dashboard/discovery
    flow, not something re-parsed as a single URL, so joining is simplest
    and keeps all the information rather than arbitrarily picking one.
    """
    if not isinstance(service_obj, dict):
        return None
    load_balancer = service_obj.get("loadBalancer")
    if not isinstance(load_balancer, dict):
        return None
    servers = load_balancer.get("servers")
    if not isinstance(servers, list):
        return None
    urls = [s["url"] for s in servers if isinstance(s, dict) and isinstance(s.get("url"), str)]
    if not urls:
        return None
    return ",".join(urls)


async def fetch_router_details(settings: Settings) -> list[RouterInfo] | None:
    """Return every Traefik HTTP router with its hostnames and backend URL.

    Does two calls — ``/api/http/routers`` then ``/api/http/services`` — to
    resolve each router's Traefik ``service`` reference to the URL(s) it
    currently load-balances to. Returns ``None`` (never raises) when
    ``TRAEFIK_API_URL`` is unset or the *routers* call fails — that's the
    data this function exists to provide, so without it there is nothing
    useful to return. A failed *services* call degrades more gently: every
    router is still returned, just with ``upstream=None``, since the
    hostname/router topology is still useful on its own.
    """
    base_url = settings.traefik_api_url.strip()
    if not base_url:
        return None
    base = base_url.rstrip("/")

    async with _traefik_client(settings) as client:
        routers = await _traefik_get(client, base + "/api/http/routers")
        if routers is None:
            return None
        services = await _traefik_get(client, base + "/api/http/services")

    if not isinstance(routers, list):
        log.warning(
            "traefik.poll_bad_response",
            url=base + "/api/http/routers",
            error="expected a JSON array",
        )
        return None

    services_by_name: dict[str, Any] = {}
    if isinstance(services, list):
        for service_obj in services:
            if isinstance(service_obj, dict) and isinstance(service_obj.get("name"), str):
                services_by_name[service_obj["name"]] = service_obj
    else:
        log.warning("traefik.services_unavailable", url=base + "/api/http/services")

    details: list[RouterInfo] = []
    for router in routers:
        if not isinstance(router, dict):
            continue
        name = router.get("name")
        rule = router.get("rule")
        if not isinstance(name, str) or not isinstance(rule, str):
            continue
        hostnames = extract_hostnames(rule)
        if not hostnames:
            continue
        service_name = router.get("service")
        upstream = (
            _resolve_service_upstream(services_by_name.get(service_name))
            if isinstance(service_name, str)
            else None
        )
        details.append(RouterInfo(name=name, hostnames=hostnames, upstream=upstream))
    return details


async def _traefik_get_text(client: httpx.AsyncClient, url: str) -> str | None:
    """Like :func:`_traefik_get` but for a plain-text body (``/metrics``)."""
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.text
    except httpx.TimeoutException as exc:
        log.warning("traefik.poll_timeout", url=url, error=str(exc))
        return None
    except httpx.HTTPError as exc:
        log.warning("traefik.poll_failed", url=url, error=str(exc))
        return None


@dataclass(frozen=True)
class RouterMetricSample:
    """One poll's worth of Prometheus counters for a single Traefik router.

    ``requests_total`` and ``by_code`` are the counter's raw cumulative value
    (summed across every ``code``/``method``/``protocol`` label combination
    for that router) — NOT a delta. ``by_code`` maps the HTTP status code
    string (e.g. ``"200"``) to its own cumulative count.
    """

    router: str
    requests_total: int
    duration_sum_s: float
    duration_count: int
    by_code: dict[str, int]


# Only these three metric families are understood — Traefik's /metrics
# exposes many more (TLS certs, entrypoint-level counters, Go runtime
# stats, ...) that this dashboard has no use for and deliberately ignores.
_ROUTER_REQUESTS_TOTAL = "traefik_router_requests_total"
_ROUTER_DURATION_SUM = "traefik_router_request_duration_seconds_sum"
_ROUTER_DURATION_COUNT = "traefik_router_request_duration_seconds_count"
_KNOWN_METRIC_FAMILIES = frozenset(
    {_ROUTER_REQUESTS_TOTAL, _ROUTER_DURATION_SUM, _ROUTER_DURATION_COUNT}
)

# ``metric_name{label="value",...} 123.45`` or the no-labels form
# ``metric_name 123.45``. A trailing Prometheus timestamp (a second number)
# is tolerated by \S+ matching only the first token after the labels.
_METRIC_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)"
)
_LABEL_RE = re.compile(r'(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"')


def parse_router_metrics(text: str) -> dict[str, RouterMetricSample]:
    """Parse the ``traefik_router_*`` families out of a Prometheus ``/metrics`` body.

    Forgiving by design: a line that doesn't match the expected shape, has a
    non-numeric value, or lacks a ``router`` label is skipped (and logged),
    never raised — one malformed line must not lose every other router's
    data. Every other metric family in the body is ignored outright.
    """
    acc: dict[str, dict[str, Any]] = {}

    def entry(router: str) -> dict[str, Any]:
        return acc.setdefault(
            router,
            {"requests_total": 0, "duration_sum_s": 0.0, "duration_count": 0, "by_code": {}},
        )

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_LINE_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if name not in _KNOWN_METRIC_FAMILIES:
            continue
        labels = dict(_LABEL_RE.findall(match.group("labels") or ""))
        router = labels.get("router")
        if not router:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            log.warning("traefik.metrics_bad_value", line=line)
            continue

        row = entry(router)
        if name == _ROUTER_REQUESTS_TOTAL:
            count = int(value)
            code = labels.get("code") or "unknown"
            row["requests_total"] += count
            row["by_code"][code] = row["by_code"].get(code, 0) + count
        elif name == _ROUTER_DURATION_SUM:
            row["duration_sum_s"] += value
        elif name == _ROUTER_DURATION_COUNT:
            row["duration_count"] += int(value)

    return {
        router: RouterMetricSample(
            router=router,
            requests_total=row["requests_total"],
            duration_sum_s=row["duration_sum_s"],
            duration_count=row["duration_count"],
            by_code=row["by_code"],
        )
        for router, row in acc.items()
    }


async def fetch_router_metrics(settings: Settings) -> dict[str, RouterMetricSample] | None:
    """Poll ``{TRAEFIK_API_URL}/metrics`` and parse it into one sample per router.

    Returns ``None`` (never raises) when ``TRAEFIK_API_URL`` is unset, the
    request fails, or Traefik was not started with
    ``--metrics.prometheus=true`` (a 404 degrades the same as any other
    failure here). An empty-but-reachable body yields ``{}``, not ``None`` —
    that is a real answer ("no routers are serving traffic"), not a failure.
    """
    base_url = settings.traefik_api_url.strip()
    if not base_url:
        return None

    url = base_url.rstrip("/") + "/metrics"
    async with _traefik_client(settings) as client:
        text = await _traefik_get_text(client, url)
    if text is None:
        return None
    return parse_router_metrics(text)


def drift_status(hostname: str, live_hostnames: set[str] | None) -> DriftStatus:
    """Classify one ``ProxyHost.hostname`` against a poll result.

    ``live_hostnames=None`` means the poll was not attempted or failed —
    reported as UNKNOWN, never conflated with MISSING (those mean different
    things to an operator: "did we even check?" vs "we checked, it's not there").
    """
    if live_hostnames is None:
        return DriftStatus.UNKNOWN
    return DriftStatus.MATCHED if hostname.lower() in live_hostnames else DriftStatus.MISSING
