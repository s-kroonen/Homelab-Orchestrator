"""What a proxy host's route really points at: one backend, or a failover, weighted or
mirroring tree of them (OR-91).

A route's service is resolved against Traefik's runtime services (``/api/http/services``,
which Traefik Manager passes through as ``GET /api/traefik/services``), recursively, so a
failover whose children are themselves failovers shows every backend with its role, its
health check, whether it passes the Host header, and the transport it uses (with its
``serverName`` when Traefik's own API lists the transports). Nothing is guessed: what the
runtime does not say is left out.

Pure: no I/O. :func:`host_model` groups every router serving one hostname (a read and a
write router by path, say), :func:`resolve` resolves one service.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

#: How deep a service tree may go before it is treated as a loop.
MAX_DEPTH = 8

SINGLE, FAILOVER, WEIGHTED, MIRRORING, INTERNAL, UNKNOWN = (
    "single",
    "failover",
    "weighted",
    "mirroring",
    "internal",
    "unknown",
)


@dataclass
class Upstream:
    """One backend server, and how it is reached."""

    url: str
    #: primary / fallback (failover), member (weighted or a balanced pool), mirror; nested
    #: composites join theirs: ``primary > fallback`` is the fallback inside the primary.
    role: str
    #: The services from the route's own down to this server's load balancer.
    via: list[str]
    weight: int | None = None
    percent: int | None = None
    pass_host_header: bool | None = None
    #: ``{"name", "server_name", "insecure_skip_verify"}``; the last two only when known.
    servers_transport: dict[str, Any] | None = None
    #: ``{"path", "interval", "timeout", "hostname"}`` as configured (missing keys unset).
    health_check: dict[str, Any] | None = None
    #: Traefik's own verdict for this server: UP / DOWN; ``None`` when not reported.
    status: str | None = None


@dataclass
class ServiceModel:
    service: str
    strategy: str
    upstreams: list[Upstream] = field(default_factory=list)
    #: One line for lists and tools: ``failover: <primary> -> <fallback>``.
    display: str = ""
    #: What could not be resolved (a missing child, a loop).
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bare(name: str) -> str:
    return name.split("@", 1)[0]


def _provider(name: str) -> str:
    return name.split("@", 1)[1] if "@" in name else ""


def index_services(runtime: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Runtime services by full name (``x@file``)."""
    return {str(s["name"]): dict(s) for s in runtime if isinstance(s, Mapping) and s.get("name")}


def _find(services: Mapping[str, dict[str, Any]], name: str, near: str) -> tuple[str, dict] | None:
    """A service by the name a router or a composite gives it. A name without a provider
    is looked up in the provider of the one that names it (Traefik's own rule), then as
    is."""
    if name in services:
        return name, services[name]
    if "@" not in name:
        for provider in (_provider(near), "file"):
            full = f"{name}@{provider}" if provider else name
            if full in services:
                return full, services[full]
    return None


def _health(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    keep = {k: raw[k] for k in ("path", "interval", "timeout", "hostname") if k in raw}
    return keep or None


def _transport(name: Any, transports: Mapping[str, Mapping[str, Any]] | None) -> dict | None:
    if not isinstance(name, str) or not name:
        return None
    out: dict[str, Any] = {"name": name}
    found = None
    if transports:
        found = (
            transports.get(name)
            or transports.get(_bare(name))
            or next((t for k, t in transports.items() if _bare(k) == _bare(name)), None)
        )
    if found is not None:
        if found.get("serverName"):
            out["server_name"] = found["serverName"]
        if "insecureSkipVerify" in found:
            out["insecure_skip_verify"] = bool(found["insecureSkipVerify"])
    return out


def _kind(svc: Mapping[str, Any]) -> str:
    for key in ("failover", "weighted", "mirroring", "loadBalancer"):
        if isinstance(svc.get(key), Mapping):
            return key
    return ""


def resolve(
    name: str,
    services: Mapping[str, dict[str, Any]],
    transports: Mapping[str, Mapping[str, Any]] | None = None,
) -> ServiceModel:
    """The tree under service ``name`` (as a router names it), flattened to its servers."""
    if name.endswith("@internal") or name in {"api@internal", "noop@internal"}:
        return ServiceModel(name, INTERNAL, display=f"internal: {name}")
    found = _find(services, name, name)
    if found is None:
        return ServiceModel(
            name, UNKNOWN, display=name, problems=[f"Traefik lists no service {name}"]
        )
    full, svc = found
    model = ServiceModel(full, UNKNOWN)
    kind = _kind(svc)
    model.strategy = {
        "failover": FAILOVER,
        "weighted": WEIGHTED,
        "mirroring": MIRRORING,
        "loadBalancer": SINGLE,
    }.get(kind, UNKNOWN)
    model.display = _walk(full, svc, services, transports, model, [], None, {}, 0)
    if model.strategy == UNKNOWN and not model.problems:
        model.problems.append(f"{full} is a kind of service this does not read")
    return model


def _walk(
    full: str,
    svc: Mapping[str, Any],
    services: Mapping[str, dict[str, Any]],
    transports: Mapping[str, Mapping[str, Any]] | None,
    model: ServiceModel,
    via: list[str],
    role: str | None,
    extra: dict[str, Any],
    depth: int,
) -> str:
    """Add ``svc``'s servers to ``model`` (with ``role`` from the composite above, if any)
    and return its display text."""
    if full in via or depth > MAX_DEPTH:
        model.problems.append(f"{' -> '.join([*via, full])} loops back on itself")
        return f"(loop at {_bare(full)})"
    path = [*via, full]
    kind = _kind(svc)

    def child(name: Any, child_role: str, child_extra: dict[str, Any] | None = None) -> str:
        if not isinstance(name, str) or not name:
            model.problems.append(f"{_bare(full)} names a child without a name")
            return "?"
        found = _find(services, name, full)
        if found is None:
            model.problems.append(f"{_bare(full)} points at {name}, which Traefik does not list")
            return f"{name} (missing)"
        cname, csvc = found
        text = _walk(
            cname, csvc, services, transports, model, path, child_role, child_extra or {}, depth + 1
        )
        return f"({text})" if _kind(csvc) in ("failover", "weighted", "mirroring") else text

    if kind == "loadBalancer":
        lb = svc["loadBalancer"]
        servers = [s for s in lb.get("servers") or [] if isinstance(s, Mapping) and s.get("url")]
        status = svc.get("serverStatus") if isinstance(svc.get("serverStatus"), Mapping) else {}
        for s in servers:
            url = str(s["url"])
            model.upstreams.append(
                Upstream(
                    url=url,
                    role=role or ("primary" if len(servers) == 1 else "member"),
                    via=path,
                    weight=extra.get("weight", s.get("weight")),
                    percent=extra.get("percent"),
                    pass_host_header=(
                        bool(lb["passHostHeader"]) if "passHostHeader" in lb else None
                    ),
                    servers_transport=_transport(lb.get("serversTransport"), transports),
                    health_check=_health(lb.get("healthCheck")),
                    status=str(status[url]) if url in status else None,
                )
            )
        urls = [str(s["url"]) for s in servers]
        return ", ".join(urls) if urls else f"{_bare(full)} (no servers)"

    def nest(inner: str) -> str:
        return inner if role is None else f"{role} > {inner}"

    if kind == "failover":
        fo = svc["failover"]
        primary = child(fo.get("service"), nest("primary"))
        fallback = child(fo.get("fallback"), nest("fallback"))
        return f"failover: {primary} -> {fallback}"
    if kind == "weighted":
        parts = []
        for entry in svc["weighted"].get("services") or []:
            if not isinstance(entry, Mapping):
                continue
            weight = entry.get("weight")
            text = child(entry.get("name"), nest("member"), {"weight": weight})
            parts.append(f"{text} x{weight}" if weight is not None else text)
        return "weighted: " + ", ".join(parts)
    if kind == "mirroring":
        mi = svc["mirroring"]
        main = child(mi.get("service"), nest("primary"))
        mirrors = []
        for entry in mi.get("mirrors") or []:
            if isinstance(entry, Mapping):
                pct = entry.get("percent")
                text = child(entry.get("name"), nest("mirror"), {"percent": pct})
                mirrors.append(f"{text} {pct}%" if pct is not None else text)
        return f"mirroring: {main}" + (" + " + ", ".join(mirrors) if mirrors else "")
    model.problems.append(f"{_bare(full)} is a kind of service this does not read")
    return _bare(full)


def host_model(
    hostname: str,
    routes: Sequence[Any] | None,
    services: Mapping[str, dict[str, Any]] | None,
    transports: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Every router serving ``hostname`` with the service tree it points at, highest
    priority first (Traefik's own order), and one line for the host. ``None`` when the
    routes or the runtime services could not be read."""
    if routes is None or services is None:
        return None
    host = hostname.strip().lower()
    mine = [
        r
        for r in routes
        if host in {h.lower() for h in r.hostnames} and getattr(r, "service", None)
    ]
    if not mine:
        return None
    mine.sort(key=lambda r: (-(getattr(r, "priority", None) or 0), r.name))
    routers = []
    for r in mine:
        model = resolve(str(r.service), services, transports)
        routers.append(
            {
                "router": r.name,
                "rule": getattr(r, "rule", "") or "",
                "priority": getattr(r, "priority", None),
                "redirect": r.name.endswith("-redirect"),
                **model.as_dict(),
            }
        )
    serving = [r for r in routers if not r["redirect"]] or routers
    if len(serving) == 1:
        display = serving[0]["display"]
    else:
        display = " | ".join(f"{r['router']}: {r['display']}" for r in serving)
    strategies = {r["strategy"] for r in serving}
    return {
        "strategy": serving[0]["strategy"] if len(strategies) == 1 else "mixed",
        "display": display,
        # For lists: the catch-all router (the lowest priority) and how many others there are.
        "short": serving[-1]["display"],
        "other_routers": len(serving) - 1,
        "routers": routers,
        "problems": [p for r in routers for p in r["problems"]],
    }


__all__ = [
    "FAILOVER",
    "INTERNAL",
    "MIRRORING",
    "SINGLE",
    "UNKNOWN",
    "WEIGHTED",
    "ServiceModel",
    "Upstream",
    "host_model",
    "index_services",
    "resolve",
]
