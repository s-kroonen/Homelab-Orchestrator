"""Everything a proxy has - routers, middlewares, services (backends) and servers transports -
with who made each one and who uses it, and the *findings*: what could be converted, removed,
simplified or moved. Read only: nothing here writes to the proxy. See docs/proxy_hosts.md,
"Inventory and findings".

The raw input is one :meth:`ProxyAdapter.read_inventory` answer (Traefik Manager's file routes
and middlewares plus Traefik's running routers, middlewares and services from every provider)
and, when the Traefik API connection is set, Traefik's servers transports.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session

from orchestrator.registry.route_standard import LEGACY_REDIRECT
from orchestrator.registry.traefik_status import extract_hostnames

#: The shared service every maintenance middleware points at, and its never-matched router.
MAINTENANCE_BACKEND = "orchestrator-maintenance-backend"

#: Decisions a person can record per finding. Recording one changes nothing in the proxy.
DECISIONS = ("later", "keep", "convert", "delete", "simplify", "move")

GROUPS = (
    ("convert", "Can be converted to the standard"),
    ("unused", "Unused"),
    ("leftover", "Leftovers"),
    ("old_syntax", "Old syntax"),
    ("look", "Worth a look"),
)


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
    #: orchestrator / hand-written / docker / internal / <provider>
    made_by: str
    redirect: bool
    proxy_host: str
    proxy_host_id: int | None


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
    suggestion: str
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
            )
        )
    backends.sort(key=lambda b: b.name)

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
    )


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
    shared_file: str = "_shared.yml",
) -> list[Finding]:
    """What the inventory says could be better, grouped as in :data:`GROUPS`."""
    out: list[Finding] = []
    slugs = set(service_slugs)
    by_name = {b.name: b for b in inv.backends}

    # Can be converted: a hand-written file router that serves a proxy host kept here.
    per_host: dict[str, list[Router]] = {}
    for r in inv.routers:
        if r.made_by == "hand-written" and r.proxy_host and not r.redirect:
            per_host.setdefault(r.proxy_host, []).append(r)
    for host, rs in sorted(per_host.items()):
        kinds = {
            by_name.get(_qualify(r.service, r.provider)).kind
            for r in rs
            if by_name.get(_qualify(r.service, r.provider))
        }
        what = next((k for k in ("failover", "weighted", "mirroring") if k in kinds), "")
        files = sorted({r.config_file for r in rs if r.config_file})
        detail = (
            f"Hand-written{' ' + what if what else ''} route"
            + (f"s ({len(rs)} routers)" if len(rs) > 1 else "")
            + (f" in {', '.join(files)}" if files else "")
            + "."
        )
        out.append(
            Finding(
                id=f"convert:{host}",
                group="convert",
                target=host,
                detail=detail,
                suggestion="convert",
                refs={"routers": [r.name for r in rs], "files": files},
            )
        )

    # Unused middlewares, services and transports (only what a file defines: Docker labels and
    # Traefik's own come and go with their containers).
    for m in inv.middlewares:
        if m.provider != "file" or m.used_by or m.on_entrypoint:
            continue
        bare = _bare(m.name)
        gone = (
            bare.startswith("maintenance-") and slugs and bare[len("maintenance-") :] not in slugs
        )
        out.append(
            Finding(
                id=f"unused:middleware:{bare}",
                group="unused",
                target=bare,
                detail=(
                    "Maintenance page for a service that no longer exists. No router uses it."
                    if gone
                    else f"{m.summary or m.type} middleware no router uses."
                ),
                suggestion="delete",
                refs={"middlewares": [m.name], "files": [m.config_file] if m.config_file else []},
            )
        )
    for b in inv.backends:
        if b.provider != "file" or b.used_by or _bare(b.name).startswith(MAINTENANCE_BACKEND):
            continue
        out.append(
            Finding(
                id=f"unused:service:{_bare(b.name)}",
                group="unused",
                target=_bare(b.name),
                detail=f"Service ({b.kind}) no router, failover or middleware points at.",
                suggestion="delete",
                refs={"services": [b.name]},
            )
        )
    for t in inv.transports:
        if t.known and not t.used_by:
            out.append(
                Finding(
                    id=f"unused:transport:{t.name}",
                    group="unused",
                    target=t.name,
                    detail="Servers transport no service uses.",
                    suggestion="delete",
                    refs={"transports": [t.name]},
                )
            )

    # Leftovers: switched off with no proxy host, no proxy host at all, two routers for one rule.
    for r in inv.routers:
        if r.provider != "file" or r.proxy_host or r.redirect:
            continue
        if any(h.endswith(".invalid") for h in r.hostnames):
            continue
        if not r.hostnames:
            continue
        off = r.status == "off"
        out.append(
            Finding(
                id=f"leftover:router:{_bare(r.name)}",
                group="leftover",
                target=_bare(r.name),
                detail=(
                    f"Switched off router for {', '.join(r.hostnames)}, with no proxy host here."
                    if off
                    else f"Router for {', '.join(r.hostnames)}, with no proxy host here."
                ),
                suggestion="delete" if off else "later",
                refs={"routers": [r.name], "files": [r.config_file] if r.config_file else []},
            )
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
        out.append(
            Finding(
                id="leftover:duplicate:" + "+".join(names),
                group="leftover",
                target=rs[0].proxy_host or ", ".join(rs[0].hostnames),
                detail=f"{len(rs)} routers have the same rule ({', '.join(names)}); only one answers.",
                suggestion="delete",
                refs={"routers": [r.name for r in rs]},
            )
        )

    # Old syntax: a redirect middleware on an HTTPS router - the redirect router does that now.
    legacy: dict[str, list[str]] = {}
    mw_by_bare = {_bare(m.name): m for m in inv.middlewares}
    for r in inv.routers:
        if not r.tls or r.redirect:
            continue
        for m in r.middlewares:
            mw = mw_by_bare.get(_bare(m))
            if mw and mw.type == "redirectScheme" and LEGACY_REDIRECT.search(_bare(m)):
                legacy.setdefault(_bare(m), []).append(r.name)
    for m, rs in sorted(legacy.items()):
        out.append(
            Finding(
                id=f"old_syntax:{m}",
                group="old_syntax",
                target=m,
                detail=(
                    f"Redirect middleware on {len(rs)} HTTPS router{'s' if len(rs) != 1 else ''}: "
                    "it does nothing there; the HTTP redirect router does this."
                ),
                suggestion="simplify",
                refs={"middlewares": [m], "routers": rs},
            )
        )

    # Worth a look.
    for b in inv.backends:
        if b.kind != "failover" or b.provider != "file":
            continue
        roles = {k["role"]: by_name.get(_qualify(str(k["name"]), b.provider)) for k in b.children}
        primary, fallback = roles.get("primary"), roles.get("fallback")
        if primary is not None and primary.kind == "loadBalancer" and not primary.health_check:
            out.append(
                Finding(
                    id=f"look:failover_unchecked:{_bare(b.name)}",
                    group="look",
                    target=_bare(b.name),
                    detail="Its primary has no health check, so Traefik never fails over.",
                    suggestion="later",
                    refs={"services": [b.name, primary.name]},
                )
            )
        if fallback is not None and fallback.kind == "loadBalancer" and not fallback.health_check:
            out.append(
                Finding(
                    id=f"look:fallback_unchecked:{_bare(b.name)}",
                    group="look",
                    target=_bare(b.name),
                    detail=(
                        "Its fallback has no health check: requests go there whenever the "
                        "primary is DOWN, even if the fallback is down too."
                    ),
                    suggestion="keep",
                    refs={"services": [b.name, fallback.name]},
                )
            )
    shared = [
        r
        for r in inv.routers
        if r.config_file == shared_file and r.proxy_host and not r.redirect and r.provider == "file"
    ]
    if shared:
        out.append(
            Finding(
                id=f"look:shared_file:{shared_file}",
                group="look",
                target=shared_file,
                detail=(
                    f"{len(shared)} route{'s' if len(shared) != 1 else ''} still live in the shared "
                    "file instead of their service's file."
                ),
                suggestion="move",
                refs={"routers": [r.name for r in shared], "files": [shared_file]},
            )
        )
    for r in inv.routers:
        if r.error and r.status in ("warning", "disabled"):
            out.append(
                Finding(
                    id=f"look:router_error:{_bare(r.name)}",
                    group="look",
                    target=_bare(r.name),
                    detail=f"Traefik reports: {r.error}",
                    suggestion="later",
                    refs={"routers": [r.name]},
                )
            )
    order = {g: i for i, (g, _l) in enumerate(GROUPS)}
    out.sort(key=lambda f: (order.get(f.group, 99), f.target))
    return out


# -- decisions --------------------------------------------------------------------------------


def _decisions_key(source: str) -> str:
    return f"proxy.findings.decisions.{source or 'default'}"


def load_decisions(session: Session, source: str) -> dict[str, dict[str, Any]]:
    from orchestrator.db.models import Setting

    row = session.get(Setting, _decisions_key(source))
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def save_decision(
    session: Session, source: str, finding_id: str, decision: str, actor: str
) -> dict[str, Any]:
    """Record what a person wants done with one finding. Changes nothing in the proxy."""
    from orchestrator.db.models import Setting

    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    key = _decisions_key(source)
    row = session.get(Setting, key) or Setting(key=key, value={})
    value = dict(row.value or {})
    entry = {"decision": decision, "by": actor, "at": datetime.now(UTC).isoformat()}
    value[finding_id] = entry
    row.value = value
    row.updated_at = datetime.now(UTC)
    session.add(row)
    return entry


# -- output -----------------------------------------------------------------------------------


def report(
    inv: Inventory, found: list[Finding], decisions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The report as plain data: counts, findings (with suggestion and decision) by group."""
    groups = []
    for key, label in GROUPS:
        items = [
            {
                **asdict(f),
                "decision": (decisions.get(f.id) or {}).get("decision") or "",
                "decided_by": (decisions.get(f.id) or {}).get("by") or "",
            }
            for f in found
            if f.group == key
        ]
        if items:
            groups.append({"key": key, "label": label, "findings": items})
    decided = [f for f in found if (decisions.get(f.id) or {}).get("decision")]
    return {
        "read_at": inv.read_at,
        "counts": {
            "routers": len(inv.routers),
            "middlewares": len(inv.middlewares),
            "services": len(inv.backends),
            "transports": len(inv.transports),
            "files": len(inv.files),
            "findings": len(found),
            "decided": len(decided),
        },
        "errors": inv.errors,
        "transports_known": inv.transports_known,
        "groups": groups,
    }


_SUGGEST = {
    "later": "Decide later",
    "keep": "Keep",
    "convert": "Convert",
    "delete": "Delete",
    "simplify": "Simplify",
    "move": "Move",
}


def to_markdown(rep: dict[str, Any], *, source: str = "") -> str:
    c = rep["counts"]
    lines = [
        f"# Proxy report{' - ' + source if source else ''}",
        "",
        f"Read {rep['read_at']}: {c['routers']} routers, {c['middlewares']} middlewares, "
        f"{c['services']} services, {c['transports']} transports, {c['files']} files. "
        f"{c['findings']} findings, {c['decided']} decided.",
    ]
    if rep["errors"]:
        lines += ["", "Not read: " + "; ".join(f"{k} ({v})" for k, v in rep["errors"].items())]
    for g in rep["groups"]:
        lines += ["", f"## {g['label']} ({len(g['findings'])})", ""]
        lines += ["| What | Finding | Suggested | Decision |", "|---|---|---|---|"]
        for f in g["findings"]:
            detail = f["detail"].replace("|", "\\|")
            lines.append(
                f"| `{f['target']}` | {detail} | {_SUGGEST.get(f['suggestion'], f['suggestion'])} "
                f"| {_SUGGEST.get(f['decision'], f['decision']) or '-'} |"
            )
    return "\n".join(lines) + "\n"


def to_json(inv: Inventory, rep: dict[str, Any]) -> str:
    return json.dumps({"inventory": asdict(inv), "report": rep}, indent=2, default=str)


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
    "DECISIONS",
    "GROUPS",
    "Backend",
    "Finding",
    "Inventory",
    "Middleware",
    "Router",
    "Transport",
    "build",
    "findings",
    "goes_to",
    "load_decisions",
    "read",
    "report",
    "save_decision",
    "to_json",
    "to_markdown",
]
