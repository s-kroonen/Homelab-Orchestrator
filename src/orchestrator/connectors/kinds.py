"""The connectors for the kinds the orchestrator has: Proxmox VE, Proxmox Backup Server,
Portainer, Traefik Manager, the Traefik API, the power manager and Home Assistant.

Each one is small: what its page shows comes from its adapter. Resource tabs with their own page
modules (Portainer's stacks, Proxmox's guests, the proxy hosts) are declared with ``view`` set to
that module; the rest uses the generic table.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from orchestrator.connectors.base import (
    Chip,
    Column,
    Connector,
    Overview,
    RawEndpoint,
    Resource,
    SourceCtx,
    Summary,
)
from orchestrator.connectors.registry import register


def _plain(value: Any) -> Any:
    """Adapter objects (dataclasses, enums) as JSON-ready data for the Raw tab."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(v) for v in value]
    if hasattr(value, "value") and type(value).__module__.startswith("orchestrator"):
        return value.value
    return value


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def _summarised(connector: Connector, ctx: SourceCtx, has: Any) -> Summary:
    """The connection check, plus what the source has when it answered."""
    summary = await Connector.summary(connector, ctx)
    if summary.state == "fail" or ctx.adapter is None:
        return summary
    try:
        summary.has = await has()
    except Exception as exc:  # the check passed but listing failed: say so, keep the state
        summary.detail = f"{type(exc).__name__}: {exc}"[:200]
    return summary


# -- Proxmox VE -------------------------------------------------------------------------------------


async def _pve_nodes(ctx: SourceCtx) -> list[dict[str, Any]]:
    status = await ctx.adapter.cluster_status()
    guests = await ctx.adapter.list_guests()
    out = []
    for n in status.nodes:
        mine = [g for g in guests if g.node == n.name]
        out.append(
            {
                "id": n.name,
                "name": n.name,
                "state": "online" if n.online else "offline",
                "guests": len(mine),
                "running": sum(1 for g in mine if g.status == "running"),
                "ip": n.ip,
                "chips": ["online" if n.online else "offline"],
            }
        )
    return out


async def _pve_guests(ctx: SourceCtx) -> list[dict[str, Any]]:
    guests = await ctx.adapter.list_guests()
    return [
        {
            "id": f"{g.node}/{g.vmid}",
            "vmid": g.vmid,
            "name": g.name,
            "node": g.node,
            "kind": g.kind.value if hasattr(g.kind, "value") else str(g.kind),
            "state": g.status,
            "chips": ["running" if g.status == "running" else "stopped"],
        }
        for g in guests
    ]


class ProxmoxConnector(Connector):
    kind = "proxmox"
    title = "Proxmox VE"
    group = "Proxmox"
    family = "machines"
    blurb = "nodes, VMs and containers"
    scripts = ("objects.js", "source_proxmox.js")
    resources = (
        Resource(
            "nodes",
            "Nodes",
            (
                Column("name", "Node"),
                Column("state", "State", "pill"),
                Column("guests", "Guests", "num"),
                Column("ip", "Address", "mono"),
            ),
            _pve_nodes,
            chips=(Chip("online", "Online"), Chip("offline", "Offline")),
            view="proxmox.nodes",
        ),
        Resource(
            "guests",
            "Guests",
            (
                Column("vmid", "VMID", "num"),
                Column("name", "Name"),
                Column("node", "Node"),
                Column("kind", "Kind"),
                Column("state", "State", "pill"),
            ),
            _pve_guests,
            chips=(Chip("running", "Running"), Chip("stopped", "Stopped")),
            view="proxmox.guests",
        ),
    )
    raw = (
        RawEndpoint(
            "cluster",
            "cluster status",
            "GET /cluster/status",
            lambda ctx: ctx.adapter.cluster_status(),
        ),
        RawEndpoint(
            "guests",
            "guests",
            "GET /cluster/resources?type=vm",
            lambda ctx: ctx.adapter.list_guests(),
        ),
        RawEndpoint(
            "storage",
            "backup storage",
            "GET /storage",
            lambda ctx: ctx.adapter.list_backup_storages(),
        ),
        RawEndpoint("version", "version", "GET /version", lambda ctx: ctx.adapter.version()),
    )

    async def summary(self, ctx: SourceCtx) -> Summary:
        async def has() -> str:
            nodes = (await ctx.adapter.cluster_status()).nodes
            guests = await ctx.adapter.list_guests()
            return f"{_count(len(nodes), 'node')} · {_count(len(guests), 'guest')}"

        return await _summarised(self, ctx, has)

    async def overview(self, ctx: SourceCtx) -> Overview:
        ov = Overview(facts=self.facts(ctx))
        if ctx.adapter is None:
            return ov
        status = await ctx.adapter.cluster_status()
        guests = await ctx.adapter.list_guests()
        online = sum(1 for n in status.nodes if n.online)
        running = sum(1 for g in guests if g.status == "running")
        ov.tiles = [
            {"label": "Nodes", "value": online, "of": len(status.nodes), "sub": "online"},
            {"label": "Guests", "value": running, "of": len(guests), "sub": "running"},
        ]
        if status.cluster_name:
            ov.facts.insert(0, ("Cluster", status.cluster_name))
        for n in status.nodes:
            if not n.online:
                ov.attention.append(
                    {"tone": "off", "label": "offline", "text": f"{n.name} is offline or asleep"}
                )
        return ov


# -- Proxmox Backup Server ----------------------------------------------------------------------------


async def _pbs_datastores(ctx: SourceCtx) -> list[dict[str, Any]]:
    out = []
    for name in await ctx.adapter.list_datastores():
        st = await ctx.adapter.datastore_status(name)
        used, total = getattr(st, "used", 0) or 0, getattr(st, "total", 0) or 0
        pct = round(100 * used / total) if total else None
        out.append(
            {
                "id": name,
                "name": name,
                "used": used,
                "total": total,
                "pct": pct,
                "chips": ["full"] if pct and pct >= 90 else [],
            }
        )
    return out


class PbsConnector(Connector):
    kind = "pbs"
    title = "Proxmox Backup Server"
    group = "Backups"
    family = "network"
    blurb = "datastores and snapshots"
    resources = (
        Resource(
            "datastores",
            "Datastores",
            (
                Column("name", "Datastore"),
                Column("pct", "Used", "pct"),
                Column("used", "Used", "bytes"),
                Column("total", "Size", "bytes"),
            ),
            _pbs_datastores,
            chips=(Chip("full", "Filling up"),),
        ),
    )
    raw = (
        RawEndpoint(
            "datastores",
            "datastores",
            "GET /admin/datastore",
            lambda ctx: ctx.adapter.list_datastores(),
        ),
        RawEndpoint("version", "version", "GET /version", lambda ctx: ctx.adapter.version()),
    )

    async def summary(self, ctx: SourceCtx) -> Summary:
        summary = await Connector.summary(self, ctx)
        if summary.state == "fail" or ctx.adapter is None:
            return summary
        try:
            stores = await _pbs_datastores(ctx)
            summary.has = _count(len(stores), "datastore")
            worst = max((s["pct"] or 0 for s in stores), default=0)
            if worst >= 90:
                summary.state, summary.label = "warn", f"datastore {worst}% full"
        except Exception as exc:
            summary.detail = str(exc)[:200]
        return summary


# -- Portainer ------------------------------------------------------------------------------------


async def _pt_environments(ctx: SourceCtx) -> list[dict[str, Any]]:
    envs = await ctx.adapter.list_environments()
    return [
        {
            "id": e.id,
            "name": e.name,
            "type": e.type,
            "state": e.status,
            "running": e.running,
            "stopped": e.stopped,
            "stacks": e.stacks,
            "chips": ["down"] if e.status == "down" else [],
        }
        for e in envs
    ]


def _pt_links(ctx: SourceCtx) -> dict[tuple[str, int, str], list[str]]:
    """(stack | container, environment, name) -> the services linked to it on this Portainer
    (a link naming no connection is on the default one)."""
    from sqlmodel import Session, select

    from orchestrator.db.models import Service
    from orchestrator.db.session import get_engine

    default = getattr(ctx.adapters, "default_portainer", "") if ctx.adapters else ""
    out: dict[tuple[str, int, str], list[str]] = {}
    with Session(get_engine()) as session:
        for svc in session.exec(select(Service)).all():
            cfg = svc.controller or {}
            if cfg.get("type") != "portainer":
                continue
            if (str(cfg.get("instance") or "") or default) != ctx.conn.id:
                continue
            try:
                eid = int(cfg["endpoint"])
            except (KeyError, TypeError, ValueError):
                continue
            for kind in ("stack", "container"):
                if cfg.get(kind):
                    out.setdefault((kind, eid, str(cfg[kind]).lower()), []).append(svc.slug)
    return out


async def _pt_up_envs(ctx: SourceCtx) -> list[Any]:
    """The environments that can be read now (down ones are left out, not shown as empty)."""
    return [e for e in await ctx.adapter.list_environments() if e.status != "down"]


def _stack_href(ctx: SourceCtx, eid: int, name: str | None) -> str:
    from urllib.parse import quote

    return f"/sources/{quote(ctx.conn.id)}/stacks/{eid}/{quote(name, safe='') if name else '-'}"


async def _pt_containers(ctx: SourceCtx) -> list[dict[str, Any]]:
    import asyncio

    from orchestrator.adapters.portainer.links import container_url

    envs = await _pt_up_envs(ctx)
    links = _pt_links(ctx)
    per_env = await asyncio.gather(
        *(ctx.adapter.list_containers(e.id) for e in envs), return_exceptions=True
    )
    rows: list[dict[str, Any]] = []
    for env, boxes in zip(envs, per_env, strict=True):
        if isinstance(boxes, BaseException):
            continue
        for c in boxes:
            linked = links.get(("container", env.id, c.name.lower())) or links.get(
                ("stack", env.id, (c.stack or "").lower()), []
            )
            state = "unhealthy" if c.state == "running" and c.health == "unhealthy" else c.state
            exit_code = re.search(r"Exited \((\d+)\)", c.status_text or "")
            chips = ["running" if c.state == "running" else "stopped"]
            if c.health == "unhealthy":
                chips.append("unhealthy")
            if not linked:
                chips.append("unlinked")
            rows.append(
                {
                    "id": f"{env.id}:{c.id}",
                    "ref": c.id,
                    "name": c.name,
                    "stack": c.stack or "",
                    "stack_href": _stack_href(ctx, env.id, c.stack),
                    "endpoint_id": env.id,
                    "environment": env.name,
                    "image": c.image,
                    "state": state,
                    "state_text": f"{state} ({exit_code.group(1)})" if exit_code else state,
                    "status_text": c.status_text,
                    "open_url": container_url(ctx.adapter.base_url, env.id, c.id),
                    "services": linked,
                    "service": linked[0] if linked else "",
                    "chips": chips,
                }
            )
    return rows


async def _pt_stacks(ctx: SourceCtx) -> list[dict[str, Any]]:
    listing = await ctx.adapter.list_stacks()
    up = {e.id: e.name for e in await _pt_up_envs(ctx)}
    links = _pt_links(ctx)
    rows = []
    for s in listing.stacks:
        if s.endpoint_id not in up or s.endpoint_id in listing.errors:
            continue
        linked = links.get(("stack", s.endpoint_id, s.name.lower()), [])
        rows.append(
            {
                "id": f"{s.endpoint_id}/{s.name}",
                "name": s.name,
                "href": _stack_href(ctx, s.endpoint_id, s.name),
                "environment": up[s.endpoint_id],
                "state": s.status,
                "type": s.type,
                "service": linked[0] if linked else "",
                "chips": [s.status] + ([] if linked else ["unlinked"]),
            }
        )
    return rows


def _docker_rows(what: str) -> Any:
    async def fetch(ctx: SourceCtx) -> list[dict[str, Any]]:
        import asyncio

        envs = await _pt_up_envs(ctx)
        got = await asyncio.gather(
            *(ctx.adapter.docker_list(e.id, what) for e in envs), return_exceptions=True
        )
        rows: list[dict[str, Any]] = []
        for env, items in zip(envs, got, strict=True):
            if isinstance(items, BaseException):
                continue
            for x in items:
                labels = x.get("Labels") or {}
                project = labels.get("com.docker.compose.project", "") if labels else ""
                if what == "images":
                    tags = [t for t in x.get("RepoTags") or [] if t != "<none>:<none>"]
                    name = tags[0] if tags else str(x.get("Id", ""))[7:19] or "untagged"
                    used = x.get("Containers")
                    row = {
                        "name": name,
                        "size": x.get("Size"),
                        "used": used if isinstance(used, int) and used >= 0 else None,
                        "chips": [] if tags else ["dangling"],
                    }
                elif what == "volumes":
                    name = str(x.get("Name", ""))
                    row = {"name": name, "driver": x.get("Driver", ""), "stack": project}
                    row["chips"] = [] if project else ["loose"]
                else:
                    name = str(x.get("Name", ""))
                    row = {"name": name, "driver": x.get("Driver", ""), "stack": project}
                    row["chips"] = ["builtin"] if name in ("bridge", "host", "none") else []
                row |= {"id": f"{env.id}:{x.get('Id') or name}", "environment": env.name}
                rows.append(row)
        return rows

    return fetch


class PortainerConnector(Connector):
    kind = "portainer"
    title = "Portainer"
    group = "Portainer"
    family = "apps"
    blurb = "environments, stacks, containers"
    scripts = ("source_portainer.js",)
    resources = (
        Resource(
            "environments",
            "Environments",
            (
                Column("name", "Environment"),
                Column("type", "Type"),
                Column("state", "State", "pill"),
                Column("stacks", "Stacks", "num"),
                Column("running", "Running", "num"),
                Column("stopped", "Stopped", "num"),
            ),
            _pt_environments,
            chips=(Chip("down", "Down"),),
        ),
        Resource(
            "stacks",
            "Stacks",
            (
                Column("name", "Stack"),
                Column("environment", "Environment"),
                Column("state", "State", "pill"),
                Column("service", "Service"),
            ),
            _pt_stacks,
            chips=(
                Chip("active", "Running"),
                Chip("inactive", "Stopped"),
                Chip("unlinked", "Not linked"),
            ),
            view="portainer.stacks",
        ),
        Resource(
            "containers",
            "Containers",
            (
                Column("name", "Container"),
                Column("stack", "Stack"),
                Column("environment", "Environment"),
                Column("image", "Image", "mono"),
                Column("state", "State", "pill"),
                Column("service", "Service"),
                Column("status_text", "Up"),
            ),
            _pt_containers,
            chips=(
                Chip("running", "Running"),
                Chip("stopped", "Stopped"),
                Chip("unhealthy", "Unhealthy"),
                Chip("unlinked", "Not linked"),
            ),
            view="portainer.containers",
        ),
        Resource(
            "images",
            "Images",
            (
                Column("name", "Image", "mono"),
                Column("environment", "Environment"),
                Column("size", "Size", "bytes"),
                Column("used", "Containers", "num"),
            ),
            _docker_rows("images"),
            chips=(Chip("dangling", "Untagged"),),
        ),
        Resource(
            "volumes",
            "Volumes",
            (
                Column("name", "Volume", "mono"),
                Column("environment", "Environment"),
                Column("stack", "Stack"),
                Column("driver", "Driver"),
            ),
            _docker_rows("volumes"),
            chips=(Chip("loose", "In no stack"),),
        ),
        Resource(
            "networks",
            "Networks",
            (
                Column("name", "Network", "mono"),
                Column("environment", "Environment"),
                Column("stack", "Stack"),
                Column("driver", "Driver"),
            ),
            _docker_rows("networks"),
            chips=(Chip("builtin", "Built in"),),
        ),
    )
    raw = (
        RawEndpoint(
            "environments",
            "environments",
            "GET /api/endpoints",
            lambda ctx: ctx.adapter.list_environments(),
        ),
        RawEndpoint("stacks", "stacks", "GET /api/stacks", lambda ctx: ctx.adapter.list_stacks()),
    )

    async def summary(self, ctx: SourceCtx) -> Summary:
        async def has() -> str:
            envs = await ctx.adapter.list_environments()
            stacks = await ctx.adapter.list_stacks()
            running = sum(e.running or 0 for e in envs)
            return (
                f"{_count(len(envs), 'environment')} · {_count(len(stacks.stacks), 'stack')}"
                f" · {running} running"
            )

        return await _summarised(self, ctx, has)


# -- Traefik Manager (the proxy) -----------------------------------------------------------------------


class ProxyConnector(Connector):
    kind = "proxy"
    title = "Traefik Manager"
    group = "Proxy"
    family = "network"
    blurb = "proxy hosts, routes and middlewares"
    raw = (
        RawEndpoint("routes", "routes", "GET /api/routes", lambda ctx: ctx.adapter.list_routes()),
        RawEndpoint(
            "middlewares",
            "middlewares",
            "GET /api/middlewares",
            lambda ctx: ctx.adapter.list_middlewares(),
        ),
        RawEndpoint(
            "files", "config files", "GET /api/configs", lambda ctx: ctx.adapter.list_config_files()
        ),
    )

    async def summary(self, ctx: SourceCtx) -> Summary:
        async def has() -> str:
            routes = await ctx.adapter.list_routes() or []
            middlewares = await ctx.adapter.list_middlewares() or []
            return f"{_count(len(routes), 'route')} · {_count(len(middlewares), 'middleware')}"

        return await _summarised(self, ctx, has)


class TraefikApiConnector(Connector):
    kind = "traefik_api"
    title = "Traefik API"
    group = "Proxy"
    family = "network"
    blurb = "Traefik's own API: routers and traffic numbers"


# -- power -------------------------------------------------------------------------------------------


class PowerConnector(Connector):
    kind = "power"
    title = "Power manager (MQTT)"
    group = "Power"
    family = "network"
    blurb = "wake and power off nodes"

    async def summary(self, ctx: SourceCtx) -> Summary:
        summary = await Connector.summary(self, ctx)
        if ctx.adapter is None or summary.state == "fail":
            return summary
        try:
            check = await ctx.adapter.check()
            online = getattr(check, "online", None)
            if isinstance(online, list | tuple | set):
                summary.has = f"{_count(len(online), 'power target')} online"
        except Exception as exc:
            summary.detail = str(exc)[:200]
        return summary


# -- Home Assistant ----------------------------------------------------------------------------------


class HaosConnector(Connector):
    kind = "haos"
    title = "Home Assistant"
    group = "Home Assistant"
    family = "apps"
    blurb = "entities a service can switch"


for _c in (
    ProxmoxConnector(),
    PbsConnector(),
    PortainerConnector(),
    ProxyConnector(),
    TraefikApiConnector(),
    PowerConnector(),
    HaosConnector(),
):
    register(_c)


_SECRET_KEY = re.compile(r"pass|secret|token|api_?key|auth|credential|private", re.I)


def redact(value: Any) -> Any:
    """Every value under a key that looks like a credential, replaced: the Raw tab shows what
    the API returns, never a password or token in it."""
    if isinstance(value, dict):
        return {
            k: ("•••" if _SECRET_KEY.search(str(k)) and v not in (None, "", [], {}) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


async def raw_payload(endpoint: RawEndpoint, ctx: SourceCtx) -> Any:
    """What an endpoint returns, as plain data (dataclasses unfolded), credentials redacted."""
    return redact(_plain(await endpoint.fetch(ctx)))


__all__ = ["raw_payload", "redact"]
