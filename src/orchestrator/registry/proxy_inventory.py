"""Everything a proxy has - routers, middlewares, services (backends) and servers transports -
with who made each one and who uses it, and the *review*: errors, what the orchestrator cannot
read yet, routes not in Traefik Manager's own form, and clean-up. Read only: nothing here writes
to the proxy, and nothing is decided or applied here - the review says what it found and how to
fix it by hand (in Traefik Manager) or with an agent. See docs/proxy_hosts.md, "Inventory and
review".

The raw input is one :meth:`ProxyAdapter.read_inventory` answer (Traefik Manager's file routes
and middlewares plus Traefik's running routers, middlewares and services from every provider)
and, when the Traefik API connection is set, Traefik's servers transports.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from orchestrator.registry.route_standard import LEGACY_REDIRECT
from orchestrator.registry.traefik_status import extract_hostnames

#: The shared service every maintenance middleware points at, and its never-matched router.
MAINTENANCE_BACKEND = "orchestrator-maintenance-backend"

GROUPS = (
    ("error", "Errors"),
    ("unread", "Not read yet"),
    ("handwritten", "Hand-written (not Traefik Manager's form)"),
    ("cleanup", "Clean-up"),
)

#: What the orchestrator does with each kind of Traefik configuration: "edits" (reads it and
#: changes it through Traefik Manager's own forms), "reads" (shows it), "not yet".
SUPPORT = (
    ("Routers with Host() rules", "edits"),
    ("Path and method rules, priority", "reads"),
    ("Failover, weighted, mirroring", "edits"),
    ("Health checks", "edits"),
    ("Host header, servers transports", "reads"),
    ("Middlewares (any type)", "reads"),
    ("Maintenance page, HSTS", "edits"),
    ("Docker and other providers", "reads"),
    ("highestRandomWeight", "not yet"),
    ("TCP and UDP routers", "not yet"),
    ("HostRegexp and other rules", "not yet"),
    ("TLS options and stores", "not yet"),
)

#: Who made a file router, judged by its shape: Traefik Manager's route form writes router
#: ``<name>`` with service ``<name>-service`` (or a failover / weighted service it manages).
FORM = "Traefik Manager"


def _bare(name: str) -> str:
    return name.split("@", 1)[0]


def _provider(name: str, fallback: str = "file") -> str:
    return name.split("@", 1)[1] if "@" in name else fallback


@dataclass
class Router:
    name: str
    provider: str
    rule: str
    hostnames: list[str]
    service: str
    middlewares: list[str]
    entry_points: list[str]
    priority: int | None
    tls: bool
    #: enabled / disabled / warning (Traefik), or "off" (switched off in Traefik Manager).
    status: str
    error: str
    config_file: str
    #: orchestrator / Traefik Manager (its route form) / hand-written / docker / internal /
    #: <provider>, judged by shape, not by name (see :data:`FORM`).
    made_by: str
    redirect: bool
    proxy_host: str
    proxy_host_id: int | None
    #: Why a file router is not in Traefik Manager's form (empty when it is).
    not_form: list[str] = field(default_factory=list)


@dataclass
class Middleware:
    name: str
    provider: str
    type: str
    summary: str
    config_file: str
    made_by: str
    used_by: list[str]
    on_entrypoint: bool
    status: str
    error: str = ""
    #: The middleware's settings, as one comparable string (two with the same are copies).
    settings: str = ""


@dataclass
class Backend:
    name: str
    provider: str
    #: loadBalancer / failover / weighted / mirroring / other
    kind: str
    servers: list[dict[str, Any]]
    children: list[dict[str, Any]]
    health_check: dict[str, Any] | None
    pass_host_header: bool | None
    transport: str
    used_by: list[str]
    managed_by_tm: bool
    status: str
    error: str = ""


@dataclass
class Transport:
    name: str
    server_name: str
    insecure: bool
    used_by: list[str]
    #: True when Traefik's own API listed it; False when only a service names it.
    known: bool


@dataclass
class Finding:
    id: str
    group: str
    target: str
    detail: str
    #: The file(s) it is in ("" when not one file).
    file: str
    #: How to fix it, by hand or with an agent. Nothing here applies it.
    fix: str
    refs: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Inventory:
    read_at: str
    routers: list[Router]
    middlewares: list[Middleware]
    backends: list[Backend]
    transports: list[Transport]
    files: list[str]
    #: Parts that could not be read: {part: reason}.
    errors: dict[str, str]
    config_errors: list[Any]
    transports_known: bool
    #: Not read into the inventory yet, kept so the review can say so.
    tcp_routers: list[dict[str, Any]] = field(default_factory=list)
    udp_routers: list[dict[str, Any]] = field(default_factory=list)
    tls_options: list[dict[str, Any]] = field(default_factory=list)
    #: Entry points that redirect every request elsewhere: {"web": "websecure"}.
    entrypoint_redirects: dict[str, str] = field(default_factory=dict)


# -- building ---------------------------------------------------------------------------------


def _mw_type(row: dict[str, Any]) -> tuple[str, str]:
    """(type, one-line summary) of a runtime middleware."""
    # Traefik's ``type`` is lower case ("redirectscheme"); the block's own key keeps the case.
    kind = next((k for k in row if isinstance(row.get(k), dict)), "") or str(row.get("type") or "")
    body = row.get(kind) if isinstance(row.get(kind), dict) else {}
    if kind == "errors":
        status = body.get("status")
        return kind, "errors · " + ", ".join(status) if isinstance(status, list) else kind
    if kind == "plugin":
        names = list(body)
        return kind, "plugin · " + (names[0] if names else "?")
    if kind == "redirectScheme":
        return kind, f"redirectScheme · {body.get('scheme', '')}".rstrip(" ·")
    return kind, kind


def _children(row: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    for kind in ("failover", "weighted", "mirroring"):
        body = row.get(kind)
        if not isinstance(body, dict):
            continue
        if kind == "failover":
            kids = [{"role": "primary", "name": body.get("service")}]
            if body.get("fallback"):
                kids.append({"role": "fallback", "name": body.get("fallback")})
        elif kind == "weighted":
            kids = [
                {"role": "member", "name": c.get("name"), "weight": c.get("weight")}
                for c in body.get("services") or []
                if isinstance(c, dict)
            ]
        else:
            kids = [{"role": "main", "name": body.get("service")}] + [
                {"role": "mirror", "name": c.get("name"), "percent": c.get("percent")}
                for c in body.get("mirrors") or []
                if isinstance(c, dict)
            ]
        return kind, [k for k in kids if k.get("name")]
    if isinstance(row.get("loadBalancer"), dict):
        return "loadBalancer", []
    if isinstance(row.get("highestRandomWeight"), dict):
        return "highestRandomWeight", []
    return str(row.get("type") or "other"), []


def _qualify(name: str, provider: str) -> str:
    return name if "@" in name else f"{name}@{provider}"


def build(
    raw: dict[str, Any],
    *,
    hosts: Iterable[Any] = (),
    owns: Callable[[str], bool] = lambda _n: False,
    transports: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> Inventory:
    """The inventory from one ``read_inventory`` answer. ``hosts``: the proxy hosts kept here
    (``hostname``, ``id``); ``owns``: whether a router name is one the orchestrator made."""
    by_host = {str(h.hostname).lower(): h for h in hosts}
    file_routes = raw.get("routes") or []
    tm_by_name: dict[str, dict[str, Any]] = {}
    for app in file_routes:
        if app.get("protocol", "http") != "http":
            continue
        name = app.get("name") or app.get("id")
        if isinstance(name, str):
            tm_by_name.setdefault(name, app)

    def host_of(hostnames: Iterable[str]) -> Any:
        return next((by_host[h.lower()] for h in sorted(hostnames) if h.lower() in by_host), None)

    routers: list[Router] = []
    seen_file: set[str] = set()
    for r in raw.get("runtime_routers") or []:
        name = str(r.get("name") or "")
        if not name:
            continue
        provider = str(r.get("provider") or _provider(name))
        bare = _bare(name)
        app = tm_by_name.get(bare) if provider == "file" else None
        if app is not None:
            seen_file.add(bare)
        rule = str(r.get("rule") or "")
        hostnames = sorted(extract_hostnames(rule))
        ph = host_of(hostnames)
        mws = [str(m) for m in r.get("middlewares") or [] if isinstance(m, str)]
        errs = r.get("error")
        made_by = (
            "internal"
            if provider == "internal"
            else (
                ("orchestrator" if owns(bare) else "hand-written")
                if provider == "file"
                else provider
            )
        )
        routers.append(
            Router(
                name=name,
                provider=provider,
                rule=rule,
                hostnames=hostnames,
                service=str(r.get("service") or ""),
                middlewares=mws,
                entry_points=[str(e) for e in r.get("entryPoints") or []],
                priority=r.get("priority") if isinstance(r.get("priority"), int) else None,
                # ``tls: {}`` is TLS with the defaults: present means on.
                tls=r.get("tls") is not None,
                status=str(r.get("status") or ""),
                error="; ".join(errs) if isinstance(errs, list) else str(errs or ""),
                config_file=str((app or {}).get("configFile") or ""),
                made_by=made_by,
                redirect=bare.endswith("-redirect"),
                proxy_host=ph.hostname if ph else "",
                proxy_host_id=getattr(ph, "id", None) if ph else None,
            )
        )
    # Switched off in Traefik Manager: still in its files' list, gone from Traefik.
    for bare, app in tm_by_name.items():
        if bare in seen_file or app.get("enabled", True) is not False:
            continue
        rule = str(app.get("rule") or "")
        hostnames = sorted(extract_hostnames(rule))
        ph = host_of(hostnames)
        routers.append(
            Router(
                name=f"{bare}@file",
                provider="file",
                rule=rule,
                hostnames=hostnames,
                service=str(app.get("service_name") or ""),
                middlewares=[str(m) for m in app.get("middlewares") or [] if isinstance(m, str)],
                entry_points=[str(e) for e in app.get("entryPoints") or []],
                priority=app.get("priority") if isinstance(app.get("priority"), int) else None,
                tls=app.get("tls") is not None and app.get("tls") is not False,
                status="off",
                error="",
                config_file=str(app.get("configFile") or ""),
                made_by="orchestrator" if owns(bare) else "hand-written",
                redirect=bare.endswith("-redirect"),
                proxy_host=ph.hostname if ph else "",
                proxy_host_id=getattr(ph, "id", None) if ph else None,
            )
        )
    routers.sort(key=lambda x: (x.proxy_host or "~", x.name))

    # Services, and who points at them (routers, parent services, errors middlewares).
    users: dict[str, set[str]] = {}

    def use(target: str, by: str, provider: str = "file") -> None:
        if target:
            users.setdefault(_qualify(target, provider), set()).add(by)

    for rt in routers:
        use(rt.service, rt.name, rt.provider)
    services_raw = raw.get("runtime_services") or []
    for s in services_raw:
        sname = str(s.get("name") or "")
        _kind, kids = _children(s)
        for k in kids:
            use(str(k["name"]), sname, _provider(sname))
    mws_raw = raw.get("runtime_middlewares") or []
    for m in mws_raw:
        body = m.get("errors")
        if isinstance(body, dict):
            use(
                str(body.get("service") or ""),
                str(m.get("name") or ""),
                _provider(str(m.get("name") or "")),
            )

    managed = {_bare(n) for n in raw.get("managed_services") or []}
    backends: list[Backend] = []
    referenced_transports: dict[str, set[str]] = {}
    for s in services_raw:
        sname = str(s.get("name") or "")
        if not sname:
            continue
        kind, kids = _children(s)
        lb = s.get("loadBalancer") if isinstance(s.get("loadBalancer"), dict) else {}
        status_map = s.get("serverStatus") if isinstance(s.get("serverStatus"), dict) else {}
        servers = [
            {"url": sv.get("url"), "status": status_map.get(sv.get("url"))}
            for sv in lb.get("servers") or []
            if isinstance(sv, dict) and sv.get("url")
        ]
        transport = str(lb.get("serversTransport") or "")
        if transport:
            referenced_transports.setdefault(_bare(transport), set()).add(sname)
        runtime_users = {str(u) for u in s.get("usedBy") or [] if isinstance(u, str)}
        backends.append(
            Backend(
                name=sname,
                provider=str(s.get("provider") or _provider(sname)),
                kind=kind,
                servers=servers,
                children=kids,
                health_check=(
                    lb.get("healthCheck") if isinstance(lb.get("healthCheck"), dict) else None
                ),
                pass_host_header=lb.get("passHostHeader") if "passHostHeader" in lb else None,
                transport=transport,
                used_by=sorted(users.get(sname, set()) | runtime_users),
                managed_by_tm=_bare(sname) in managed,
                status=str(s.get("status") or ""),
                error=_errors(s.get("err") or s.get("error")),
            )
        )
    backends.sort(key=lambda b: b.name)
    _judge_shapes(routers, {b.name: b for b in backends}, managed, owns)

    # Middlewares, with the routers (and chains) that use them.
    file_mws = {
        str(m.get("name")): str(m.get("configFile") or "")
        for m in raw.get("file_middlewares") or []
        if m.get("name")
    }
    on_eps = {_bare(m) for m in raw.get("entrypoint_middlewares") or []}
    mw_users: dict[str, set[str]] = {}
    for rt in routers:
        for m in rt.middlewares:
            mw_users.setdefault(_qualify(m, rt.provider), set()).add(rt.name)
    for m in mws_raw:
        chain = m.get("chain")
        if isinstance(chain, dict):
            for c in chain.get("middlewares") or []:
                mw_users.setdefault(
                    _qualify(str(c), _provider(str(m.get("name") or ""))), set()
                ).add(str(m.get("name")))
    middlewares: list[Middleware] = []
    for m in mws_raw:
        name = str(m.get("name") or "")
        if not name:
            continue
        provider = str(m.get("provider") or _provider(name))
        bare = _bare(name)
        kind, summary = _mw_type(m)
        runtime_users = {str(u) for u in m.get("usedBy") or [] if isinstance(u, str)}
        middlewares.append(
            Middleware(
                name=name,
                provider=provider,
                type=kind,
                summary=summary,
                config_file=file_mws.get(bare, ""),
                made_by=(
                    "orchestrator"
                    if provider == "file" and bare.startswith("maintenance-")
                    else ("hand-written" if provider == "file" else provider)
                ),
                used_by=sorted(mw_users.get(name, set()) | runtime_users),
                on_entrypoint=bare in on_eps or name in on_eps,
                status=str(m.get("status") or ""),
                error=_errors(m.get("err") or m.get("error")),
                settings=(
                    json.dumps(m.get(kind), sort_keys=True, default=str)
                    if isinstance(m.get(kind), dict)
                    else ""
                ),
            )
        )
    middlewares.sort(key=lambda x: x.name)

    out_transports: list[Transport] = []
    known = transports is not None
    names = set(referenced_transports) | ({_bare(n) for n in transports} if transports else set())
    for t in sorted(names):
        listed = t in (transports or {}) or f"{t}@file" in (transports or {})
        info = (transports or {}).get(t) or (transports or {}).get(f"{t}@file") or {}
        out_transports.append(
            Transport(
                name=t,
                server_name=str(info.get("serverName") or ""),
                insecure=bool(info.get("insecureSkipVerify")),
                used_by=sorted(referenced_transports.get(t, set())),
                known=listed,
            )
        )

    files = sorted(
        {r.config_file for r in routers if r.config_file} | set(file_mws.values()) - {""}
    )
    errors = dict(raw.get("errors") or {})
    for part in ("routes", "runtime_routers", "runtime_middlewares", "runtime_services"):
        if raw.get(part) is None:
            errors.setdefault(part, "could not be read")
    return Inventory(
        read_at=(now or datetime.now(UTC)).isoformat(),
        routers=routers,
        middlewares=middlewares,
        backends=backends,
        transports=out_transports,
        files=files,
        errors=errors,
        config_errors=list(raw.get("config_errors") or []),
        transports_known=known,
        tcp_routers=list(raw.get("runtime_tcp_routers") or []),
        udp_routers=list(raw.get("runtime_udp_routers") or []),
        tls_options=list(raw.get("tls_options") or []),
        entrypoint_redirects=dict(raw.get("entrypoint_redirects") or {}),
    )


def _errors(value: Any) -> str:
    return "; ".join(str(v) for v in value) if isinstance(value, list) else str(value or "")


#: Traefik's own services: a router that points at one is not something a route form makes.
_INTERNAL = "@internal"


def _judge_shapes(
    routers: list[Router],
    backends: dict[str, Backend],
    managed: set[str],
    owns: Callable[[str], bool],
) -> None:
    """Set each file router's ``made_by`` by its shape: Traefik Manager's route form writes
    router ``<name>`` with service ``<name>-service``, or points it at a failover / weighted
    service it manages. Anything else is hand-written, with the reasons in ``not_form``."""
    for r in routers:
        if r.provider != "file":
            continue
        bare = _bare(r.name)
        mine = owns(bare)
        if r.redirect:
            # Traefik Manager makes no redirect routers: it redirects on the entry point.
            r.made_by = "orchestrator" if mine else "hand-written"
            if not mine:
                r.not_form.append("a separate HTTP redirect router")
            continue
        svc = r.service
        reasons: list[str] = []
        if svc.endswith(_INTERNAL):
            reasons.append(f"points at Traefik's own {svc}")
        elif _bare(svc) != f"{bare}-service" and _bare(svc) not in managed:
            reasons.append(f"service {_bare(svc)} is not {bare}-service")
        b = backends.get(_qualify(svc, "file"))
        if b is not None and b.kind in ("failover", "weighted", "mirroring"):
            kids = [backends.get(_qualify(str(k["name"]), "file")) for k in b.children]
            looks = {
                (
                    k.pass_host_header,
                    _bare(k.transport) if k.transport else "",
                    (k.health_check or {}).get("hostname", ""),
                )
                for k in kids
                if k is not None and k.kind == "loadBalancer"
            }
            if len(looks) > 1:
                reasons.append(
                    "its backends need different Host headers or server names (Traefik "
                    "Manager's form gives every backend the same)"
                )
        elif b is not None and b.transport and _bare(b.transport) != f"{bare}-transport":
            reasons.append(f"transport {_bare(b.transport)} is not {bare}-transport")
        r.not_form = reasons
        r.made_by = ("orchestrator" if mine else FORM) if not reasons else "hand-written"


# -- findings ---------------------------------------------------------------------------------


def _short(b: Backend, by_name: dict[str, Backend]) -> str:
    if b.kind == "loadBalancer":
        return ", ".join(str(s["url"]) for s in b.servers) or b.name
    parts = []
    for k in b.children:
        child = by_name.get(_qualify(str(k["name"]), b.provider))
        parts.append(_short(child, by_name) if child else str(k["name"]))
    sep = " -> " if b.kind == "failover" else ", "
    return f"{b.kind}: " + sep.join(parts)


def goes_to(inv: Inventory, router: Router) -> str:
    """One line for where a router sends its requests."""
    by_name = {b.name: b for b in inv.backends}
    b = by_name.get(_qualify(router.service, router.provider))
    return _short(b, by_name) if b else router.service


def findings(
    inv: Inventory,
    *,
    service_slugs: Iterable[str] = (),
    service_of: dict[str, str] | None = None,
    shared_file: str = "_shared.yml",
) -> list[Finding]:
    """What the review reports, in the groups of :data:`GROUPS`: errors, what is not read yet,
    routes not in Traefik Manager's own form, and clean-up. ``service_of``: a proxy host's
    hostname -> the slug of the service it is linked to (names the maintenance page to keep)."""
    out: list[Finding] = []
    slugs = set(service_slugs)
    linked = {k.lower(): v for k, v in (service_of or {}).items()}
    by_name = {b.name: b for b in inv.backends}
    mw_by_bare = {_bare(m.name): m for m in inv.middlewares}

    def add(
        group: str, key: str, target: str, detail: str, fix: str, file: str = "", **refs: Any
    ) -> None:
        out.append(
            Finding(
                id=f"{group}:{key}",
                group=group,
                target=target,
                detail=detail,
                file=file,
                fix=fix,
                refs={k: list(v) for k, v in refs.items() if v},
            )
        )

    # -- errors --------------------------------------------------------------------------------
    for e in inv.config_errors:
        f = str(e.get("file") if isinstance(e, dict) else e)
        msg = str(e.get("error") if isinstance(e, dict) else "")
        add(
            "error",
            f"parse:{f}",
            f,
            f"Traefik Manager cannot parse this file{': ' + msg if msg else ''}. Its routes are "
            "not loaded.",
            "Fix the YAML in Traefik Manager's Raw editor.",
            file=f,
            files=[f],
        )
    for part, why in sorted(inv.errors.items()):
        add(
            "error",
            f"read:{part}",
            part.replace("runtime_", "running ").replace("_", " "),
            f"Could not be read: {why}. This review is incomplete.",
            "Check the connection to Traefik Manager (and Traefik), then Read again.",
        )
    for r in inv.routers:
        if r.error:
            add(
                "error",
                f"router:{_bare(r.name)}",
                _bare(r.name),
                f"Traefik reports: {r.error}",
                "Point it at what exists, or delete it in Traefik Manager.",
                file=r.config_file,
                routers=[r.name],
            )
    for m in inv.middlewares:
        if m.error:
            add(
                "error",
                f"middleware:{_bare(m.name)}",
                _bare(m.name),
                f"Traefik reports: {m.error}",
                "Fix its settings in Traefik Manager.",
                file=m.config_file,
                middlewares=[m.name],
            )
    for b in inv.backends:
        if b.error:
            add(
                "error",
                f"service:{_bare(b.name)}",
                _bare(b.name),
                f"Traefik reports: {b.error}",
                "Fix it in Traefik Manager.",
                services=[b.name],
            )

    # -- not read yet ---------------------------------------------------------------------------
    for proto, rows in (("TCP", inv.tcp_routers), ("UDP", inv.udp_routers)):
        for r in rows:
            name = str(r.get("name") or "")
            add(
                "unread",
                f"{proto.lower()}:{name}",
                _bare(name),
                f"{proto} router{' (' + str(r.get('rule')) + ')' if r.get('rule') else ''}: TCP and "
                "UDP routers are not read yet.",
                "Shown here only; manage it in Traefik Manager.",
            )
    for b in inv.backends:
        if b.kind == "highestRandomWeight":
            add(
                "unread",
                f"hrw:{_bare(b.name)}",
                _bare(b.name),
                "highestRandomWeight service: not understood yet, so its members are not shown.",
                "Shown here only; manage it in Traefik Manager.",
                services=[b.name],
            )
    for r in inv.routers:
        if r.hostnames or r.provider == "internal" or not r.rule:
            continue
        add(
            "unread",
            f"rule:{_bare(r.name)}",
            _bare(r.name),
            f"Rule {r.rule}: no hostname can be read from it, so no proxy host matches it.",
            "Use Host() rules for what the orchestrator should know, or manage it in Traefik "
            "Manager.",
            file=r.config_file,
            routers=[r.name],
        )
    if inv.tls_options:
        names = sorted({str(o.get("name")) for o in inv.tls_options})
        add(
            "unread",
            "tls_options",
            "TLS options: " + ", ".join(names),
            "TLS options and stores are not read yet.",
            "Shown here only.",
            file=", ".join(
                sorted({str(o.get("configFile") or "") for o in inv.tls_options} - {""})
            ),
        )

    # -- hand-written: hosts kept here whose routes are not in Traefik Manager's form ----------
    per_host: dict[str, list[Router]] = {}
    for r in inv.routers:
        if r.provider == "file" and r.proxy_host and not r.redirect and r.made_by == "hand-written":
            per_host.setdefault(r.proxy_host, []).append(r)
    for host, rs in sorted(per_host.items()):
        all_routers = [
            x
            for x in inv.routers
            if x.provider == "file" and x.proxy_host == host and not x.redirect
        ]
        reasons = sorted({why for r in rs for why in r.not_form})
        files = sorted({r.config_file for r in rs if r.config_file})
        stays = any("Traefik's own" in w or "different Host headers" in w for w in reasons)
        composite = any(
            _kind_of(by_name, r.service) in ("failover", "weighted", "mirroring")
            for r in all_routers
        )
        custom = len(all_routers) > 1 or any(
            r.priority is not None and not _plain_host_rule(r.rule) for r in all_routers
        )
        what = (
            f"{len(all_routers)} routers ("
            + ", ".join(
                f"{_bare(r.name)}{' priority ' + str(r.priority) if r.priority and not _plain_host_rule(r.rule) else ''}"
                for r in all_routers
            )
            + ")"
            if len(all_routers) > 1
            else f"router {_bare(all_routers[0].name)}"
        )
        if stays:
            fix = "Stays hand-written: read only here; edit it in Traefik Manager's Raw editor."
        else:
            fix = (
                "Traefik Manager's form: "
                + (
                    f"{len(all_routers)} routes on the host"
                    if len(all_routers) > 1
                    else "one route"
                )
                + (", each with its own rule and priority" if custom else "")
                + (", with failover or weighted backends" if composite else "")
                + "; then Import or Adopt it here."
            )
        add(
            "handwritten",
            host,
            host,
            f"{what[:1].upper() + what[1:]}: " + "; ".join(reasons) + ".",
            fix,
            file=", ".join(files),
            routers=[r.name for r in all_routers],
            files=files,
        )

    # -- clean-up ----------------------------------------------------------------------------
    redirects = [r for r in inv.routers if r.provider == "file" and r.redirect]
    if redirects:
        eps = sorted({ep for r in redirects for ep in r.entry_points}) or ["web"]
        done = all(ep in inv.entrypoint_redirects for ep in eps)
        add(
            "cleanup",
            "redirect_routers",
            "Redirect routers",
            f"{len(redirects)} *-redirect router{'s' if len(redirects) != 1 else ''}"
            + (
                f", while the {', '.join(eps)} entry point already redirects to HTTPS."
                if done
                else f", while the {', '.join(eps)} entry point has no redirect."
            ),
            (
                "Delete them in Traefik Manager."
                if done
                else "Traefik Manager's way: one redirect on the entry point (its static settings); then "
                "the redirect routers and their middleware can go."
            ),
            file=_files({r.config_file for r in redirects}),
            routers=[r.name for r in redirects],
        )
        with_page = [
            r for r in redirects if any(_bare(m).startswith("maintenance-") for m in r.middlewares)
        ]
        if with_page:
            add(
                "cleanup",
                "redirect_maintenance",
                "Maintenance on redirect routers",
                f"{len(with_page)} redirect router{'s' if len(with_page) != 1 else ''} carry a "
                "maintenance page, which never shows there (they never reach a backend).",
                "Remove the maintenance middleware from them.",
                routers=[r.name for r in with_page],
            )
    doubled: dict[str, set[str]] = {}
    doubled_routers: dict[str, list[str]] = {}
    for r in inv.routers:
        pages = [_bare(m) for m in r.middlewares if _bare(m).startswith("maintenance-")]
        if len(pages) > 1 and not r.redirect:
            key = r.proxy_host or _bare(r.name)
            doubled.setdefault(key, set()).update(pages)
            doubled_routers.setdefault(key, []).append(r.name)
    for key, pages in sorted(doubled.items()):
        keep = linked.get(key.lower())
        add(
            "cleanup",
            f"double_maintenance:{key}",
            key,
            f"{len(pages)} maintenance pages on its routers ({', '.join(sorted(pages))}); only the "
            "first one listed ever shows.",
            (
                f"Keep only maintenance-{keep}, the service it is linked to."
                if keep
                else "Keep only the page of the service it is linked to."
            ),
            routers=doubled_routers[key],
        )
    same_settings: dict[tuple[str, str], list[Middleware]] = {}
    for m in inv.middlewares:
        if m.provider == "file" and m.settings and m.type != "errors":
            same_settings.setdefault((m.type, m.settings), []).append(m)
    for (kind, _settings), ms in sorted(same_settings.items(), key=lambda kv: kv[0][0]):
        if len(ms) < 2:
            continue
        names = sorted(_bare(m.name) for m in ms)
        add(
            "cleanup",
            f"copies:{kind}:{names[0]}",
            f"{len(ms)} {kind} middlewares",
            f"{len(ms)} {kind} middlewares with the same settings: {', '.join(names)}.",
            "One shared middleware"
            + (", or Traefik Manager's per-route headers" if kind == "headers" else "")
            + ".",
            file=_files({m.config_file for m in ms}),
            middlewares=[m.name for m in ms],
        )
    for m in inv.middlewares:
        if m.provider != "file" or m.used_by or m.on_entrypoint:
            continue
        bare = _bare(m.name)
        gone = (
            bare.startswith("maintenance-") and slugs and bare[len("maintenance-") :] not in slugs
        )
        add(
            "cleanup",
            f"unused:middleware:{bare}",
            bare,
            (
                "Maintenance page for a service that no longer exists; no router uses it."
                if gone
                else f"{m.summary or m.type} middleware no router uses."
            ),
            "Delete it in Traefik Manager.",
            file=m.config_file,
            middlewares=[m.name],
        )
    for b in inv.backends:
        if b.provider != "file" or b.used_by or _bare(b.name).startswith(MAINTENANCE_BACKEND):
            continue
        add(
            "cleanup",
            f"unused:service:{_bare(b.name)}",
            _bare(b.name),
            f"Service ({b.kind}) no router, failover or middleware points at.",
            "Delete it in Traefik Manager.",
            services=[b.name],
        )
    for t in inv.transports:
        if t.known and not t.used_by:
            add(
                "cleanup",
                f"unused:transport:{t.name}",
                t.name,
                "Servers transport no service uses.",
                "Delete it in Traefik Manager.",
                transports=[t.name],
            )
    # Routers with no proxy host here, grouped per file (a file switched off as a whole).
    hostless: dict[str, list[Router]] = {}
    for r in inv.routers:
        if r.provider != "file" or r.proxy_host or not r.hostnames:
            continue
        if any(h.endswith(".invalid") for h in r.hostnames):
            continue
        hostless.setdefault(r.config_file or _bare(r.name), []).append(r)
    for f, rs in sorted(hostless.items()):
        mains = [r for r in rs if not r.redirect]
        on = [r for r in mains if r.status != "off"]
        hosts = sorted({h for r in rs for h in r.hostnames})
        redirect_on = [r for r in rs if r.redirect and r.status != "off"]
        if not mains:
            continue
        if on:
            add(
                "cleanup",
                f"no_host:{f}",
                ", ".join(_bare(r.name) for r in on),
                f"Serving {', '.join(hosts)}, with no proxy host here.",
                "Import it here if the orchestrator should know it, or delete it in Traefik Manager.",
                file=f,
                routers=[r.name for r in rs],
            )
        else:
            add(
                "cleanup",
                f"off:{f}",
                ", ".join(_bare(r.name) for r in mains),
                f"Switched off ({', '.join(hosts)}), with no proxy host here"
                + ("; its HTTP redirect router is still on." if redirect_on else "."),
                "Delete it in Traefik Manager.",
                file=f,
                routers=[r.name for r in rs],
            )
    same: dict[str, list[Router]] = {}
    for r in inv.routers:
        if r.redirect or r.status == "off" or not r.rule:
            continue
        same.setdefault(" ".join(r.rule.split()), []).append(r)
    for _rule, rs in sorted(same.items()):
        if len(rs) < 2:
            continue
        names = sorted(_bare(r.name) for r in rs)
        prios = {r.priority for r in rs}
        add(
            "cleanup",
            "duplicate:" + "+".join(names),
            rs[0].proxy_host or ", ".join(rs[0].hostnames),
            f"{len(rs)} routers with the same rule ({', '.join(names)})"
            + (
                " and the same priority: which one answers is not defined."
                if len(prios) == 1
                else "; only one answers."
            ),
            "Delete the one you do not need in Traefik Manager.",
            file=", ".join(sorted({r.config_file for r in rs if r.config_file})),
            routers=[r.name for r in rs],
        )
    legacy: dict[str, list[str]] = {}
    for r in inv.routers:
        if not r.tls or r.redirect:
            continue
        for m in r.middlewares:
            mw = mw_by_bare.get(_bare(m))
            if mw and mw.type == "redirectScheme" and LEGACY_REDIRECT.search(_bare(m)):
                legacy.setdefault(_bare(m), []).append(r.name)
    for m, rs in sorted(legacy.items()):
        add(
            "cleanup",
            f"https_redirect:{m}",
            m,
            f"Redirect middleware on {len(rs)} HTTPS router{'s' if len(rs) != 1 else ''}, where it "
            "does nothing.",
            "Remove it from those routers.",
            middlewares=[m],
            routers=rs,
        )
    for b in inv.backends:
        if b.kind != "failover" or b.provider != "file":
            continue
        roles = {k["role"]: by_name.get(_qualify(str(k["name"]), b.provider)) for k in b.children}
        primary, fallback = roles.get("primary"), roles.get("fallback")
        if primary is not None and primary.kind == "loadBalancer" and not primary.health_check:
            add(
                "cleanup",
                f"failover_unchecked:{_bare(b.name)}",
                _bare(b.name),
                "Its primary has no health check, so Traefik never fails over.",
                "Add a health check to the primary.",
                services=[b.name, primary.name],
            )
        if fallback is not None and fallback.kind == "loadBalancer" and not fallback.health_check:
            add(
                "cleanup",
                f"fallback_unchecked:{_bare(b.name)}",
                _bare(b.name),
                "Its fallback has no health check: requests go there whenever the primary is "
                "DOWN, even if the fallback is down too.",
                "Add a health check, unless the application handles that itself.",
                services=[b.name, fallback.name],
            )
    shared = [
        r
        for r in inv.routers
        if r.config_file == shared_file and r.proxy_host and not r.redirect and r.provider == "file"
    ]
    if shared:
        add(
            "cleanup",
            f"shared_file:{shared_file}",
            shared_file,
            f"{len(shared)} route{'s' if len(shared) != 1 else ''} still in the shared file instead "
            "of their service's file.",
            "Move them to their service's file.",
            file=shared_file,
            routers=[r.name for r in shared],
        )
    order = {g: i for i, (g, _l) in enumerate(GROUPS)}
    out.sort(key=lambda f: (order.get(f.group, 99), f.target))
    return out


def linked_services(session: Any, hosts: Iterable[Any]) -> dict[str, str]:
    """A proxy host's hostname -> the slug of the service it is linked to."""
    from sqlmodel import select

    from orchestrator.db.models import Service

    slugs = {s.id: s.slug for s in session.exec(select(Service)).all()}
    return {
        str(h.hostname): slugs[h.service_id]
        for h in hosts
        if getattr(h, "service_id", None) in slugs
    }


def _files(names: set[str]) -> str:
    """The file column: one file by name, several as a count."""
    names = {n for n in names if n}
    if len(names) <= 1:
        return next(iter(names), "")
    return f"{len(names)} files"


def _kind_of(by_name: dict[str, Backend], service: str) -> str:
    b = by_name.get(_qualify(service, "file"))
    return b.kind if b else ""


def _plain_host_rule(rule: str) -> bool:
    """``Host(`a`)`` (or several ``||``-ed): what a route form writes by default."""
    return bool(rule) and all(_HOST_ONLY.fullmatch(part.strip()) for part in rule.split("||"))


_HOST_ONLY = re.compile(r"Host\(\s*`[^`]+`\s*\)")


# -- output -----------------------------------------------------------------------------------


def report(inv: Inventory, found: list[Finding]) -> dict[str, Any]:
    """The review as plain data: counts, what is supported, and the findings by group."""
    groups = []
    for key, label in GROUPS:
        items = [asdict(f) for f in found if f.group == key]
        if items:
            groups.append({"key": key, "label": label, "findings": items})
    return {
        "read_at": inv.read_at,
        "counts": {
            "routers": len(inv.routers),
            "middlewares": len(inv.middlewares),
            "services": len(inv.backends),
            "transports": len(inv.transports),
            "files": len(inv.files),
            "findings": len(found),
            **{key: sum(1 for f in found if f.group == key) for key, _l in GROUPS},
        },
        "errors": inv.errors,
        "transports_known": inv.transports_known,
        "entrypoint_redirects": inv.entrypoint_redirects,
        "support": [{"what": w, "status": st} for w, st in SUPPORT],
        "groups": groups,
    }


def to_markdown(rep: dict[str, Any], *, source: str = "") -> str:
    """The review as Markdown: the data, what the orchestrator supports and how to fix each
    item, for a person or an agent to work from."""
    c = rep["counts"]

    def cell(text: str) -> str:
        return str(text).replace("|", "\\|").replace("\n", " ")

    lines = [
        f"# Proxy review{' - ' + source if source else ''}",
        "",
        f"Read {rep['read_at']}: {c['routers']} routers, {c['middlewares']} middlewares, "
        f"{c['services']} services, {c['transports']} transports, {c['files']} files. "
        f"{c['findings']} findings. Read only: nothing was changed.",
    ]
    if rep["errors"]:
        lines += ["", "Not read: " + "; ".join(f"{k} ({v})" for k, v in rep["errors"].items())]
    if not rep["transports_known"]:
        lines += ["", "Servers transports are known by name only (no Traefik API connection)."]
    redirects = rep.get("entrypoint_redirects") or {}
    lines += [
        "",
        "Entry point redirects: "
        + (", ".join(f"{k} -> {v}" for k, v in redirects.items()) if redirects else "none"),
        "",
        "## What the orchestrator supports",
        "",
        "| Configuration | Status |",
        "|---|---|",
    ]
    lines += [f"| {s['what']} | {s['status']} |" for s in rep["support"]]
    lines += [
        "",
        "*edits*: read and changed through Traefik Manager's own forms; *reads*: shown only; "
        "*not yet*: not read. Traefik Manager's form writes router `<name>` with service "
        "`<name>-service` (a failover or weighted service it manages, transport "
        "`<name>-transport`); HTTP to HTTPS is one redirect on the entry point.",
    ]
    for g in rep["groups"]:
        lines += ["", f"## {g['label']} ({len(g['findings'])})", ""]
        lines += ["| What | Finding | File | How to fix |", "|---|---|---|---|"]
        for f in g["findings"]:
            lines.append(
                f"| `{cell(f['target'])}` | {cell(f['detail'])} | {cell(f['file']) or '-'} "
                f"| {cell(f['fix'])} |"
            )
    return "\n".join(lines) + "\n"


def to_json(inv: Inventory, rep: dict[str, Any]) -> str:
    return json.dumps({"inventory": asdict(inv), "review": rep}, indent=2, default=str)


# -- reading, with a short cache ---------------------------------------------------------------

_CACHE: dict[str, tuple[float, Inventory]] = {}
CACHE_S = 20.0


async def read(
    adapter: Any,
    *,
    source: str,
    hosts: Iterable[Any],
    settings: Any = None,
    fresh: bool = False,
) -> Inventory | None:
    """Read the proxy (at most once per :data:`CACHE_S` per source unless ``fresh``)."""
    hit = _CACHE.get(source)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    raw = await adapter.read_inventory()
    if raw is None:
        return None
    transports = None
    if settings is not None and str(getattr(settings, "traefik_api_url", "") or "").strip():
        from orchestrator.registry.traefik_status import fetch_servers_transports

        transports = await fetch_servers_transports(settings)
    inv = build(raw, hosts=hosts, owns=adapter.owns_route_ref, transports=transports)
    _CACHE[source] = (time.monotonic(), inv)
    return inv


__all__ = [
    "GROUPS",
    "SUPPORT",
    "Backend",
    "Finding",
    "Inventory",
    "Middleware",
    "Router",
    "Transport",
    "build",
    "findings",
    "goes_to",
    "linked_services",
    "read",
    "report",
    "to_json",
    "to_markdown",
]
