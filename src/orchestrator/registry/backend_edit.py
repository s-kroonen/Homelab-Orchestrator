"""Edit a proxy host that has several backends (failover, weighted, mirroring) (OR-91).

Only such hosts, and only services Traefik reads from its file provider. What can change:
a backend service's server URLs and health check (path, interval, timeout, hostname; or
none), which backend of a failover is the primary and which the fallback, the weights of a
weighted service and the percentages of a mirroring one.

Every write goes through Traefik Manager's own service form (``POST /api/services``,
:meth:`ProxyAdapter.save_service`), which edits the service where it is in its file - the
keys it is sent replace theirs, every other key and the rest of the file stay, comments
included - and backs the file up first. A composite service is marked as managed by
Traefik Manager first (:meth:`ProxyAdapter.adopt_service`; its settings only). Nothing is
written to a file directly.

Each service carries a ``version`` (a fingerprint of what Traefik runs now): an edit names
the version it was made against and is refused when the service changed since. After a
write, Traefik is read again until the change shows (:func:`verify`).

Pure apart from :func:`apply` and :func:`verify`, which take the proxy adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from orchestrator.registry.service_model import (
    FAILOVER,
    MIRRORING,
    WEIGHTED,
    host_model,
    index_services,
    resolve,
)

#: Strategies whose hosts can be edited here.
EDITABLE_STRATEGIES = (FAILOVER, WEIGHTED, MIRRORING, "mixed")

#: Traefik's own health-check defaults: the runtime shows them even when the file does not
#: say them, so they are left out of what is written back (same behaviour, no new lines).
HC_DEFAULTS: dict[str, Any] = {
    "mode": "http",
    "interval": "30s",
    "timeout": "5s",
    "followRedirects": True,
}
#: The health-check keys Traefik Manager's form understands (anything else it keeps).
HC_FIELDS = (
    "path",
    "interval",
    "unhealthyInterval",
    "timeout",
    "scheme",
    "mode",
    "hostname",
    "method",
    "port",
    "status",
    "followRedirects",
    "headers",
)
#: What the editor offers to change in a health check.
HC_EDITABLE = ("path", "interval", "timeout", "hostname")

_URL = re.compile(r"^https?://[^\s/:]+(:\d{1,5})?(/\S*)?$")
_DURATION = re.compile(r"^\d+(\.\d+)?(ns|us|µs|ms|s|m|h)$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class EditError(ValueError):
    """An edit that cannot be made, with the reason a person can act on."""


def _bare(name: str) -> str:
    return name.split("@", 1)[0]


def _kind(svc: Mapping[str, Any]) -> str:
    for key in ("failover", "weighted", "mirroring", "loadBalancer"):
        if isinstance(svc.get(key), Mapping):
            return key
    return ""


def version_of(svc: Mapping[str, Any]) -> str:
    """A fingerprint of what Traefik runs for one service (its health verdicts left out)."""
    block = {k: svc.get(k) for k in ("loadBalancer", "failover", "weighted", "mirroring")}
    raw = json.dumps(block, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def file_health_check(raw: Any) -> dict[str, Any] | None:
    """A runtime health check as it would be written: Traefik's defaults left out."""
    if not isinstance(raw, Mapping):
        return None
    out = {k: raw[k] for k in HC_FIELDS if k in raw and raw[k] not in (None, "", {})}
    for key, default in HC_DEFAULTS.items():
        if out.get(key) == default:
            out.pop(key)
    return out


@dataclass
class Node:
    """One service of a host's tree that can be edited."""

    service: str  # Traefik's full name, ``x@file``
    name: str  # as Traefik Manager knows it
    kind: str  # loadBalancer / failover / weighted / mirroring
    version: str
    servers: list[str] = field(default_factory=list)
    #: Present when it has one (Traefik's defaults left out).
    health_check: dict[str, Any] | None = None
    #: Per-server settings this form would drop (weight, preservePath...): servers locked.
    servers_locked: str = ""
    primary: str = ""
    fallback: str = ""
    members: list[dict[str, Any]] = field(default_factory=list)  # weighted: name, weight
    main: str = ""
    mirrors: list[dict[str, Any]] = field(default_factory=list)  # mirroring: name, percent

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _node(full: str, svc: Mapping[str, Any]) -> Node | None:
    kind = _kind(svc)
    if not kind or not full.endswith("@file"):
        return None
    node = Node(service=full, name=_bare(full), kind=kind, version=version_of(svc))
    block = svc[kind]
    if kind == "loadBalancer":
        servers = [s for s in block.get("servers") or [] if isinstance(s, Mapping)]
        node.servers = [str(s.get("url")) for s in servers if s.get("url")]
        extra = sorted(
            {
                k
                for s in servers
                for k in s
                if k != "url" and not (k == "weight" and s[k] in (1, None))
            }
        )
        if extra:
            node.servers_locked = "its servers have " + ", ".join(extra)
        node.health_check = file_health_check(block.get("healthCheck"))
    elif kind == "failover":
        node.primary = str(block.get("service") or "")
        node.fallback = str(block.get("fallback") or "")
    elif kind == "weighted":
        node.members = [
            {"name": str(e.get("name")), "weight": e.get("weight", 1)}
            for e in block.get("services") or []
            if isinstance(e, Mapping) and e.get("name")
        ]
    elif kind == "mirroring":
        node.main = str(block.get("service") or "")
        node.mirrors = [
            {"name": str(e.get("name")), "percent": e.get("percent", 0)}
            for e in block.get("mirrors") or []
            if isinstance(e, Mapping) and e.get("name")
        ]
    return node


def _tree(full: str, services: Mapping[str, dict[str, Any]], seen: list[str]) -> None:
    """Collect ``full`` and everything under it into ``seen`` (in order, once)."""
    if full in seen or full not in services:
        return
    seen.append(full)
    svc = services[full]
    kind = _kind(svc)
    block = svc.get(kind) if kind else None
    names: list[str] = []
    if kind == "failover":
        names = [block.get("service"), block.get("fallback")]
    elif kind == "weighted":
        names = [e.get("name") for e in block.get("services") or [] if isinstance(e, Mapping)]
    elif kind == "mirroring":
        names = [block.get("service")] + [
            e.get("name") for e in block.get("mirrors") or [] if isinstance(e, Mapping)
        ]
    for child in names:
        if not isinstance(child, str) or not child:
            continue
        found = _lookup(services, child, full)
        if found:
            _tree(found, services, seen)


def _lookup(services: Mapping[str, Any], name: str, near: str) -> str | None:
    if name in services:
        return name
    if "@" not in name:
        provider = near.split("@", 1)[1] if "@" in near else "file"
        for full in (f"{name}@{provider}", f"{name}@file"):
            if full in services:
                return full
    return None


@dataclass
class Editable:
    hostname: str
    editable: bool
    reason: str = ""
    strategy: str = ""
    nodes: list[Node] = field(default_factory=list)
    #: Services a failover may point at: every HTTP service of the file provider.
    choices: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "editable": self.editable,
            "reason": self.reason,
            "strategy": self.strategy,
            "nodes": [n.as_dict() for n in self.nodes],
            "choices": self.choices,
        }


def editable(
    hostname: str,
    routes: Sequence[Any] | None,
    runtime: Sequence[Mapping[str, Any]] | Mapping[str, dict[str, Any]] | None,
) -> Editable:
    """What of ``hostname``'s backends can be edited, with each service's version."""
    if routes is None or runtime is None:
        return Editable(hostname, False, "Traefik Manager or Traefik could not be read")
    services = runtime if isinstance(runtime, Mapping) else index_services(runtime)
    model = host_model(hostname, routes, services)
    if model is None:
        return Editable(hostname, False, "no route serves this hostname")
    if model["strategy"] not in EDITABLE_STRATEGIES:
        return Editable(
            hostname,
            False,
            "it has one backend: edit it as a proxy host",
            strategy=model["strategy"],
        )
    seen: list[str] = []
    for router in model["routers"]:
        if not router["redirect"]:
            _tree(router["service"], services, seen)
    nodes = [n for n in (_node(full, services[full]) for full in seen) if n is not None]
    choices = sorted(_bare(k) for k, v in services.items() if k.endswith("@file") and _kind(v))
    return Editable(hostname, True, strategy=model["strategy"], nodes=nodes, choices=choices)


# --- planning --------------------------------------------------------------------------------


@dataclass
class Plan:
    #: ``{"service", "what", "before", "after"}`` - what a person confirms.
    changes: list[dict[str, Any]] = field(default_factory=list)
    #: ``{"service", "adopt", "payload"}`` - what is sent, in order.
    calls: list[dict[str, Any]] = field(default_factory=list)


def _check_url(url: str) -> str:
    url = str(url or "").strip()
    if not _URL.match(url):
        raise EditError(f"{url or '(empty)'} is not an http:// or https:// address")
    return url


def _check_hc(fields: Mapping[str, Any]) -> None:
    path = fields.get("path")
    if path is not None and not str(path).startswith("/"):
        raise EditError(f"the health-check path {path!r} must start with /")
    for key in ("interval", "timeout"):
        value = fields.get(key)
        if value is not None and not _DURATION.match(str(value)):
            raise EditError(f"the health-check {key} {value!r} is not a duration like 10s")
    host = fields.get("hostname")
    if host is not None and not _HOSTNAME.match(str(host)):
        raise EditError(f"{host!r} is not a hostname")


def _hc_payload(hc: Mapping[str, Any] | None) -> dict[str, Any]:
    if hc is None:
        return {"enabled": False}
    out: dict[str, Any] = {"enabled": True}
    for key, value in hc.items():
        if key == "followRedirects":
            if value is False:
                out[key] = False
        else:
            out[key] = value
    return out


def plan(found: Editable, edits: Sequence[Mapping[str, Any]], runtime: Any) -> Plan:
    """Turn ``edits`` into what is sent and what a person confirms. Raises
    :class:`EditError` for the first edit that cannot be made, so nothing is half-sent."""
    if not found.editable:
        raise EditError(f"{found.hostname} cannot be edited here: {found.reason}")
    by_name = {n.name: n for n in found.nodes} | {n.service: n for n in found.nodes}
    services = runtime if isinstance(runtime, Mapping) else index_services(runtime)
    out = Plan()
    trial = {k: dict(v) for k, v in services.items()}
    for edit in edits:
        name = str(edit.get("service") or "")
        node = by_name.get(name)
        if node is None:
            raise EditError(f"{name or '(no name)'} is not a service of {found.hostname}")
        if edit.get("version") and edit["version"] != node.version:
            raise EditError(
                f"{node.name} changed in Traefik since it was read: reopen it and make the "
                "change again"
            )
        if node.kind == "loadBalancer":
            _plan_lb(node, edit, out)
        elif node.kind == "failover":
            _plan_failover(node, edit, out, found, trial)
        elif node.kind == "weighted":
            _plan_weighted(node, edit, out)
        elif node.kind == "mirroring":
            _plan_mirroring(node, edit, out)
    if not out.calls:
        raise EditError("nothing to change")
    return out


def _plan_lb(node: Node, edit: Mapping[str, Any], out: Plan) -> None:
    servers = node.servers
    if "servers" in edit:
        servers = [_check_url(u) for u in edit.get("servers") or []]
        if not servers:
            raise EditError(f"{node.name} needs at least one server")
        if servers != node.servers:
            if node.servers_locked:
                raise EditError(
                    f"{node.name}'s servers cannot be changed here: {node.servers_locked}, "
                    "which this form would drop"
                )
            out.changes.append(
                {"service": node.name, "what": "servers", "before": node.servers, "after": servers}
            )
    hc_changed = False
    hc = node.health_check
    if "health_check" in edit:
        asked = edit.get("health_check")
        if asked is None:
            new_hc = None
        else:
            new_hc = dict(hc or {})
            for key in HC_EDITABLE:
                if key in asked:
                    value = asked[key]
                    if value in (None, ""):
                        new_hc.pop(key, None)
                    else:
                        new_hc[key] = str(value).strip()
            _check_hc(new_hc)
            new_hc = file_health_check(new_hc) or {}  # Traefik's defaults go unsaid
            if not new_hc.get("path"):
                raise EditError(f"{node.name}'s health check needs a path")
        if new_hc != hc:
            hc_changed = True
            out.changes.append(
                {"service": node.name, "what": "health check", "before": hc, "after": new_hc}
            )
            hc = new_hc
    if servers == node.servers and not hc_changed:
        return
    payload: dict[str, Any] = {
        "name": node.name,
        "type": "loadBalancer",
        "children": [{"kind": "manual", "address": u} for u in servers],
    }
    if hc_changed:
        payload["healthCheck"] = _hc_payload(hc)
    out.calls.append({"service": node.name, "adopt": False, "payload": payload})


def _plan_failover(
    node: Node, edit: Mapping[str, Any], out: Plan, found: Editable, trial: dict[str, dict]
) -> None:
    primary = str(edit.get("primary") or node.primary).strip()
    fallback = str(edit.get("fallback") or node.fallback).strip()
    if (primary, fallback) == (node.primary, node.fallback):
        return
    for which, value in (("primary", primary), ("fallback", fallback)):
        if _bare(value) not in found.choices:
            raise EditError(f"{value} is not a service Traefik reads from its files")
        if _bare(value) == node.name:
            raise EditError(f"{node.name} cannot be its own {which}")
    if _bare(primary) == _bare(fallback):
        raise EditError("the primary and the fallback must be different services")
    trial[node.service] = {"failover": {"service": primary, "fallback": fallback}}
    loops = [p for p in resolve(node.service, trial).problems if "loops" in p]
    if loops:
        raise EditError(loops[0])
    out.changes.append(
        {
            "service": node.name,
            "what": "failover",
            "before": {"primary": node.primary, "fallback": node.fallback},
            "after": {"primary": primary, "fallback": fallback},
        }
    )
    out.calls.append(
        {
            "service": node.name,
            "adopt": True,
            "payload": {
                "name": node.name,
                "type": "failover",
                "children": [
                    {"kind": "service", "name": primary},
                    {"kind": "service", "name": fallback},
                ],
            },
        }
    )


def _int_in(value: Any, low: int, high: int, what: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise EditError(f"{what} {value!r} is not a whole number") from None
    if not low <= out <= high:
        raise EditError(f"{what} must be between {low} and {high}")
    return out


def _plan_weighted(node: Node, edit: Mapping[str, Any], out: Plan) -> None:
    asked = edit.get("weights") or {}
    unknown = [k for k in asked if k not in {m["name"] for m in node.members}]
    if unknown:
        raise EditError(f"{unknown[0]} is not a member of {node.name}")
    after = [
        {
            "name": m["name"],
            "weight": _int_in(asked.get(m["name"], m["weight"]), 0, 1000, f"{m['name']}'s weight"),
        }
        for m in node.members
    ]
    if after == node.members:
        return
    if not any(m["weight"] for m in after):
        raise EditError("at least one member needs a weight above 0")
    out.changes.append(
        {"service": node.name, "what": "weights", "before": node.members, "after": after}
    )
    out.calls.append(
        {
            "service": node.name,
            "adopt": True,
            "payload": {
                "name": node.name,
                "type": "weighted",
                "children": [
                    {"kind": "service", "name": m["name"], "weight": m["weight"]} for m in after
                ],
            },
        }
    )


def _plan_mirroring(node: Node, edit: Mapping[str, Any], out: Plan) -> None:
    asked = edit.get("mirrors") or {}
    unknown = [k for k in asked if k not in {m["name"] for m in node.mirrors}]
    if unknown:
        raise EditError(f"{unknown[0]} is not a mirror of {node.name}")
    after = [
        {
            "name": m["name"],
            "percent": _int_in(asked.get(m["name"], m["percent"]), 0, 100, f"{m['name']}'s share"),
        }
        for m in node.mirrors
    ]
    if after == node.mirrors:
        return
    out.changes.append(
        {"service": node.name, "what": "mirrors", "before": node.mirrors, "after": after}
    )
    out.calls.append(
        {
            "service": node.name,
            "adopt": True,
            "payload": {
                "name": node.name,
                "type": "mirroring",
                "children": [{"kind": "service", "name": node.main}]
                + [{"kind": "service", "name": m["name"], "percent": m["percent"]} for m in after],
            },
        }
    )


# --- applying ------------------------------------------------------------------------------------


async def apply(proxy: Any, the_plan: Plan) -> list[dict[str, Any]]:
    """Send the plan, one service at a time, stopping at the first refusal (what was sent
    before it stays sent; the result says which)."""
    results: list[dict[str, Any]] = []
    for call in the_plan.calls:
        try:
            if call["adopt"]:
                await proxy.adopt_service(call["service"])
            await proxy.save_service(call["payload"])
        except Exception as exc:
            results.append({"service": call["service"], "ok": False, "message": str(exc)})
            break
        results.append({"service": call["service"], "ok": True, "message": "saved"})
    return results


async def verify(
    read: Callable[[], Awaitable[Any]],
    hostname: str,
    routes: Sequence[Any],
    the_plan: Plan,
    *,
    attempts: int = 8,
    wait_s: float = 1.5,
) -> bool:
    """Read Traefik again until every planned change shows (Traefik reloads its files on its
    own, usually within a second or two). ``False`` when it does not show in time."""
    for attempt in range(attempts):
        runtime = await read()
        if runtime is not None and _shows(editable(hostname, routes, runtime), the_plan):
            return True
        if attempt < attempts - 1:
            await asyncio.sleep(wait_s)
    return False


def _shows(found: Editable, the_plan: Plan) -> bool:
    nodes = {n.name: n for n in found.nodes}
    for change in the_plan.changes:
        node = nodes.get(change["service"])
        if node is None:
            return False
        what, after = change["what"], change["after"]
        now: Any
        if what == "servers":
            now = node.servers
        elif what == "health check":
            now = node.health_check
        elif what == "failover":
            now = {"primary": node.primary, "fallback": node.fallback}
            after = {k: _bare(v) for k, v in after.items()}
            now = {k: _bare(v) for k, v in now.items()}
        elif what == "weights":
            now = node.members
        else:
            now = node.mirrors
        if now != after:
            return False
    return True


__all__ = [
    "EDITABLE_STRATEGIES",
    "EditError",
    "Editable",
    "Node",
    "Plan",
    "apply",
    "editable",
    "file_health_check",
    "plan",
    "verify",
    "version_of",
]
