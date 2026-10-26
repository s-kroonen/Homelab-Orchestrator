"""What a proxy route looks like when Traefik Manager (and this orchestrator) create it from
scratch, and how to tell whether an existing route can be rewritten into that shape safely.

The *standard* is exactly what the new-host form offers and what ``create_route`` writes: a
router named ``orchestrator-<host>`` in the configured config file, bound to the HTTPS entry
point(s), with the certificate resolver, the visitor's Host header passed through, and a
companion plain-HTTP -> HTTPS redirect router when Traefik has the redirect middleware. Hand-
written and NPM-migrated routes carry other syntax (their own names, a redirect middleware on the
HTTPS router, HSTS and friends); rewriting them is only safe when nothing they say would be lost,
which :func:`analyze` decides from the route's stored YAML.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

from ruamel.yaml import YAML

from orchestrator.adapters.proxy.base import RouteOptions

_safe = YAML(typ="safe")

#: Middleware names that only exist to bounce plain HTTP to HTTPS (an NPM "Force SSL" artifact):
#: the companion redirect router replaces them, so they are dropped from the rewritten route.
LEGACY_REDIRECT = re.compile(r"redirect|https|ssl|scheme|force", re.IGNORECASE)

_ROUTER_KEYS = {"rule", "entryPoints", "service", "tls", "middlewares"}
_TLS_KEYS = {"certResolver", "domains"}
_LB_KEYS = {"servers", "passHostHeader", "serversTransport"}
_HOST_RE = re.compile(r"Host\(\s*`([^`]+)`\s*\)")


def standard_options(
    existing: dict[str, Any] | None,
    *,
    redirect_available: bool | None,
    skip_verify: bool | None = None,
    tls_main: str | None = None,
    tls_sans: tuple[str, ...] | None = None,
) -> RouteOptions:
    """The options a route is rewritten with: what the host already says about its certificate
    and backend TLS, with the Host header passed through and the HTTP -> HTTPS redirect on when
    Traefik has the middleware for it (the new-host form's own defaults)."""
    base = RouteOptions.from_dict(existing)
    return RouteOptions(
        pass_host_header=True,
        skip_upstream_tls_verify=(
            base.skip_upstream_tls_verify if skip_verify is None else skip_verify
        ),
        tls_main=base.tls_main if tls_main is None else tls_main,
        tls_sans=base.tls_sans if tls_sans is None else tls_sans,
        http_redirect=redirect_available is True,
        config_file=base.config_file,
    )


@dataclass
class RouteAnalysis:
    """What a route's stored YAML says, and whether any of it cannot be recreated."""

    name: str = ""
    rule: str = ""
    hosts: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    cert_resolver: str = ""
    tls_main: str = ""
    tls_sans: tuple[str, ...] = ()
    middlewares: list[str] = field(default_factory=list)
    upstream: str = ""
    pass_host_header: bool | None = None
    skip_verify: bool = False
    #: Things the rewrite would lose or cannot express: the route is left alone when any exist.
    problems: list[str] = field(default_factory=list)

    @property
    def simple(self) -> bool:
        return not self.problems

    @property
    def legacy_middlewares(self) -> list[str]:
        return [m for m in self.middlewares if LEGACY_REDIRECT.search(m)]


def analyze(raw: str, hostname: str) -> RouteAnalysis:
    """Read one route's stored YAML (``http: {routers: {name: ...}, services: ...}``)."""
    out = RouteAnalysis()
    try:
        doc = _safe.load(io.StringIO(raw)) or {}
    except Exception as exc:
        out.problems.append(f"its configuration could not be read ({type(exc).__name__})")
        return out
    http = doc.get("http") if isinstance(doc, dict) else None
    if not isinstance(http, dict):
        out.problems.append("its configuration has no http section")
        return out
    routers = http.get("routers") or {}
    if len(routers) != 1:
        out.problems.append(f"its file entry holds {len(routers)} routers, not one")
        return out
    name, router = next(iter(routers.items()))
    out.name = str(name)
    if not isinstance(router, dict):
        out.problems.append("the router is not a mapping")
        return out

    out.rule = str(router.get("rule") or "")
    out.hosts = _HOST_RE.findall(out.rule)
    if not out.hosts or re.sub(r"\s+", "", _HOST_RE.sub("", out.rule)) not in {"", "||"}:
        out.problems.append(f"its rule is not just a host: {out.rule}")
    elif [h.lower() for h in out.hosts] != [hostname.lower()]:
        out.problems.append(f"its rule matches {', '.join(out.hosts)}, not only {hostname}")
    out.entry_points = [str(e) for e in router.get("entryPoints") or []]
    out.middlewares = [str(m) for m in router.get("middlewares") or []]
    extra = set(router) - _ROUTER_KEYS
    if extra:
        out.problems.append(f"it sets {', '.join(sorted(extra))} on the router")

    tls = router.get("tls")
    if isinstance(tls, dict):
        out.cert_resolver = str(tls.get("certResolver") or "")
        if set(tls) - _TLS_KEYS:
            out.problems.append(f"its tls block sets {', '.join(sorted(set(tls) - _TLS_KEYS))}")
        domains = tls.get("domains") or []
        if len(domains) > 1:
            out.problems.append("it asks for more than one certificate domain set")
        elif domains and isinstance(domains[0], dict):
            out.tls_main = str(domains[0].get("main") or "")
            out.tls_sans = tuple(str(s) for s in domains[0].get("sans") or [])

    service = router.get("service")
    services = http.get("services") or {}
    if isinstance(service, str) and "@" in service:
        out.problems.append(f"it uses the shared service {service}")
    elif isinstance(service, str) and service in services:
        _read_service(out, services[service], http)
    elif service and services:
        out.problems.append(f"its service {service} is not in the same entry")
    return out


def _read_service(out: RouteAnalysis, svc: Any, http: dict[str, Any]) -> None:
    lb = svc.get("loadBalancer") if isinstance(svc, dict) else None
    if not isinstance(lb, dict) or set(svc) - {"loadBalancer"}:
        out.problems.append("its service is not a plain load balancer")
        return
    if set(lb) - _LB_KEYS:
        out.problems.append(f"its service sets {', '.join(sorted(set(lb) - _LB_KEYS))}")
    servers = lb.get("servers") or []
    if len(servers) != 1:
        out.problems.append(f"it balances {len(servers)} servers, not one")
    elif isinstance(servers[0], dict):
        out.upstream = str(servers[0].get("url") or "")
    if "passHostHeader" in lb:
        out.pass_host_header = bool(lb["passHostHeader"])
    transport = lb.get("serversTransport")
    if transport:
        known = (http.get("serversTransports") or {}).get(str(transport).split("@")[0])
        if isinstance(known, dict) and set(known) <= {"insecureSkipVerify"}:
            out.skip_verify = bool(known.get("insecureSkipVerify"))
        else:
            out.problems.append(f"it uses the server transport {transport}")
