"""Read-only drift detection against a live Traefik instance.

This is deliberately NOT an :mod:`orchestrator.adapters` adapter: there is
nothing to dry-run about reading Traefik's current router list, no lifecycle
(``start``/``stop``) worth managing, and no mutation path at all — the
orchestrator must never attempt to write to Traefik (see ``docs/proxy_hosts.md``).
It is a single short-lived GET, used to flag ``ProxyHost`` records that have no
matching live Traefik router.

Enable with ``TRAEFIK_API_URL`` (Traefik started with ``--api=true``). Left
unset, :func:`fetch_router_hostnames` is never called with a usable URL and
every host reads as :class:`DriftStatus.UNKNOWN` — this feature is fully
optional, the same way the PBS/Proxmox adapters degrade when unconfigured.

Failures (DNS, connect, TLS, timeout, non-2xx, bad JSON) are caught here and
turned into the ``UNKNOWN`` sentinel rather than raised — a Traefik outage or
misconfiguration must never crash a dashboard page load or an unrelated
request.
"""

from __future__ import annotations

import re
from enum import StrEnum

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


async def fetch_router_hostnames(settings: Settings) -> set[str] | None:
    """Return every hostname Traefik currently has an HTTP router for.

    Returns ``None`` (never raises) when drift checking is disabled
    (``TRAEFIK_API_URL`` unset) or the poll could not be completed.
    """
    base_url = settings.traefik_api_url.strip()
    if not base_url:
        return None

    auth = None
    if settings.traefik_api_username and settings.traefik_api_password.get_secret_value():
        auth = (settings.traefik_api_username, settings.traefik_api_password.get_secret_value())

    url = base_url.rstrip("/") + "/api/http/routers"
    try:
        async with httpx.AsyncClient(
            verify=settings.traefik_api_verify_tls,
            timeout=settings.traefik_api_timeout_s,
            auth=auth,
        ) as client:
            response = await client.get(url)
        response.raise_for_status()
        routers = response.json()
    except httpx.TimeoutException as exc:
        log.warning("traefik.poll_timeout", url=url, error=str(exc))
        return None
    except httpx.HTTPError as exc:
        log.warning("traefik.poll_failed", url=url, error=str(exc))
        return None
    except ValueError as exc:  # non-JSON body
        log.warning("traefik.poll_bad_response", url=url, error=str(exc))
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


def drift_status(hostname: str, live_hostnames: set[str] | None) -> DriftStatus:
    """Classify one ``ProxyHost.hostname`` against a poll result.

    ``live_hostnames=None`` means the poll was not attempted or failed —
    reported as UNKNOWN, never conflated with MISSING (those mean different
    things to an operator: "did we even check?" vs "we checked, it's not there").
    """
    if live_hostnames is None:
        return DriftStatus.UNKNOWN
    return DriftStatus.MATCHED if hostname.lower() in live_hostnames else DriftStatus.MISSING
