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
    Action,
    ActionResult,
    Chip,
    Column,
    Connector,
    Overview,
    RawEndpoint,
    Resource,
    SourceCtx,
    Summary,
    Tab,
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


def _pct(used: Any, total: Any) -> int | None:
    try:
        return round(100 * float(used) / float(total)) if float(total) else None
    except (TypeError, ValueError):
        return None


async def _pve_storage(ctx: SourceCtx) -> list[dict[str, Any]]:
    rows = await ctx.adapter.cluster_resources("storage")
    out = []
    for r in rows:
        pct = _pct(r.get("disk"), r.get("maxdisk"))
        chips = []
        if pct is not None and pct >= 85:
            chips.append("full")
        if r.get("status") not in (None, "available"):
            chips.append("unavailable")
        out.append(
            {
                "id": f"{r.get('node', '')}/{r.get('storage', '')}",
                "name": r.get("storage", ""),
                "node": r.get("node", ""),
                "type": r.get("plugintype", ""),
                "state": r.get("status", ""),
                "pct": pct,
                "used": r.get("disk"),
                "total": r.get("maxdisk"),
                "shared": "shared" if r.get("shared") else "",
                "chips": chips,
            }
        )
    return sorted(out, key=lambda r: (r["node"], r["name"]))


async def _pve_backup_jobs(ctx: SourceCtx) -> list[dict[str, Any]]:
    out = []
    for j in await ctx.adapter.backup_jobs():
        on = bool(j.get("enabled", 1))
        what = "every guest" if j.get("all") else f"guests {j.get('vmid', '')}".strip()
        if j.get("exclude"):
            what += f", except {j['exclude']}"
        out.append(
            {
                "id": str(j.get("id", "")),
                "name": str(j.get("id", "")),
                "schedule": j.get("schedule", ""),
                "storage": j.get("storage", ""),
                "what": what,
                "mode": j.get("mode", ""),
                "state": "on" if on else "off",
                "chips": [] if on else ["off"],
            }
        )
    return out


async def _pve_tasks(ctx: SourceCtx) -> list[dict[str, Any]]:
    from datetime import UTC, datetime

    def at(ts: Any) -> str | None:
        try:
            return datetime.fromtimestamp(int(ts), UTC).isoformat()
        except (TypeError, ValueError):
            return None

    out = []
    for t in await ctx.adapter.recent_tasks():
        status = str(t.get("status", "") or ("running" if not t.get("endtime") else ""))
        failed = status not in ("OK", "running", "")
        out.append(
            {
                "id": str(t.get("upid", "")),
                "name": f"{t.get('type', '')} {t.get('id', '')}".strip(),
                "node": t.get("node", ""),
                "user": t.get("user", ""),
                "started": at(t.get("starttime")),
                "state": "failed" if failed else ("running" if status == "running" else "ok"),
                "status": status,
                "chips": ["failed"] if failed else (["running"] if status == "running" else []),
            }
        )
    return sorted(out, key=lambda r: r["started"] or "", reverse=True)


def _pve_node_names(ctx: SourceCtx) -> set[str]:
    """The names of the nodes this Proxmox owns (the default one also has the unowned)."""
    from sqlmodel import Session

    from orchestrator.connectors.ownership import nodes_of
    from orchestrator.db.session import get_engine

    with Session(get_engine()) as session:
        return {n.name for n in nodes_of(session, ctx.conn.id)}


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
        Resource(
            "storage",
            "Storage",
            (
                Column("name", "Storage", "mono"),
                Column("node", "Node"),
                Column("type", "Type"),
                Column("pct", "Used", "pct"),
                Column("used", "Used", "bytes"),
                Column("total", "Size", "bytes"),
                Column("shared", "Shared"),
            ),
            _pve_storage,
            chips=(Chip("full", "Filling up"), Chip("unavailable", "Unavailable")),
        ),
        Resource(
            "backup_jobs",
            "Backup jobs",
            (
                Column("name", "Job", "mono"),
                Column("schedule", "Schedule", "mono"),
                Column("what", "What"),
                Column("storage", "Storage"),
                Column("mode", "Mode"),
                Column("state", "State", "pill"),
            ),
            _pve_backup_jobs,
            chips=(Chip("off", "Switched off"),),
        ),
        Resource(
            "tasks",
            "Tasks",
            (
                Column("name", "Task", "mono"),
                Column("node", "Node"),
                Column("user", "By"),
                Column("started", "Started", "ago"),
                Column("state", "Result", "pill"),
                Column("status", "Status"),
            ),
            _pve_tasks,
            chips=(Chip("failed", "Failed"), Chip("running", "Running")),
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
        RawEndpoint(
            "resources",
            "node usage",
            "GET /cluster/resources?type=node",
            lambda ctx: ctx.adapter.cluster_resources("node"),
        ),
        RawEndpoint(
            "backup",
            "backup jobs",
            "GET /cluster/backup",
            lambda ctx: ctx.adapter.backup_jobs(),
        ),
        RawEndpoint("tasks", "tasks", "GET /cluster/tasks", lambda ctx: ctx.adapter.recent_tasks()),
        RawEndpoint("version", "version", "GET /version", lambda ctx: ctx.adapter.version()),
    )

    def about(self, ctx: SourceCtx, target: str, details: dict[str, Any]) -> bool:
        if Connector.about(self, ctx, target, details):
            return True
        kind, _, rest = target.partition(":")
        if kind not in ("node", "guest"):
            return False
        return rest.split("/")[0] in _pve_node_names(ctx)

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
        usage = await ctx.adapter.cluster_resources("node")
        cpus = [float(u["cpu"]) for u in usage if u.get("cpu") is not None]
        mem = [(u.get("mem") or 0, u.get("maxmem") or 0) for u in usage]
        if cpus:
            ov.tiles.append({"label": "CPU", "value": f"{round(100 * sum(cpus) / len(cpus))}%"})
        mem_pct = _pct(sum(m for m, _ in mem), sum(t for _, t in mem))
        if mem_pct is not None:
            ov.tiles.append({"label": "Memory", "value": f"{mem_pct}%"})
        stores = [r for r in await _pve_storage(ctx) if r["pct"] is not None]
        if stores:
            worst = max(stores, key=lambda r: r["pct"])
            tone = "fail" if worst["pct"] >= 95 else "warn" if worst["pct"] >= 85 else None
            ov.tiles.append(
                {
                    "label": "Fullest storage",
                    "value": f"{worst['pct']}%",
                    "sub": f"{worst['name']} on {worst['node']}",
                    "tone": tone,
                }
            )
            if tone:
                ov.attention.append(
                    {
                        "tone": tone,
                        "label": f"{worst['pct']}% full",
                        "text": f"{worst['name']} on {worst['node']} is filling up",
                        "href": "?tab=storage",
                        "action": "Open storage",
                    }
                )
        by_node = {u.get("node"): u for u in usage}
        ov.sections.append(
            {
                "title": "Nodes",
                "columns": [
                    {"key": "name", "label": "Node", "kind": "text"},
                    {"key": "state", "label": "State", "kind": "pill"},
                    {"key": "guests", "label": "Guests", "kind": "num"},
                    {"key": "cpu", "label": "CPU", "kind": "pct"},
                    {"key": "mem", "label": "Memory", "kind": "pct"},
                ],
                "rows": [
                    {
                        "id": n.name,
                        "name": n.name,
                        "state": "online" if n.online else "offline",
                        "guests": sum(1 for g in guests if g.node == n.name),
                        "cpu": (
                            round(100 * float(by_node[n.name]["cpu"]))
                            if by_node.get(n.name, {}).get("cpu") is not None
                            else None
                        ),
                        "mem": _pct(
                            by_node.get(n.name, {}).get("mem"),
                            by_node.get(n.name, {}).get("maxmem"),
                        ),
                    }
                    for n in status.nodes
                ],
                "href": "?tab=nodes",
            }
        )
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


async def _pbs_snapshots(ctx: SourceCtx) -> list[dict[str, Any]]:
    from datetime import UTC, datetime

    rows = []
    for store in await ctx.adapter.list_datastores():
        for snap in await ctx.adapter.list_snapshots(store):
            chips = ["verified" if snap.verified else "unverified"]
            if snap.protected:
                chips.append("protected")
            rows.append(
                {
                    "id": f"{store}:{snap.snapshot_id}",
                    "name": snap.snapshot_id,
                    "group": snap.group,
                    "datastore": store,
                    "taken": datetime.fromtimestamp(snap.backup_time, UTC).isoformat(),
                    "size": snap.size_bytes,
                    "state": "verified" if snap.verified else "not verified",
                    "protected": "protected" if snap.protected else "",
                    "chips": chips,
                    # what the actions need
                    "_type": snap.backup_type,
                    "_bid": snap.backup_id,
                    "_time": snap.backup_time,
                }
            )
    return sorted(rows, key=lambda r: r["taken"], reverse=True)


async def _pbs_act(
    ctx: SourceCtx, action: str, rows: list[dict[str, Any]], actor: str
) -> list[ActionResult]:
    """Verify, protect or unprotect snapshots, each audited."""
    out = []
    for r in rows:
        where = {"backup_type": r["_type"], "backup_id": r["_bid"], "backup_time": r["_time"]}
        try:
            if action == "verify":
                ok = await ctx.adapter.verify_snapshot(r["datastore"], **where)
                message = "verified" if ok else "verify failed"
            else:
                await ctx.adapter.set_protected(
                    r["datastore"], **where, protected=action == "protect"
                )
                ok, message = True, ("protected" if action == "protect" else "unprotected")
        except Exception as exc:
            ok, message = False, f"{type(exc).__name__}: {exc}"[:200]
        _audit(actor, f"pbs.snapshot_{action}", f"snapshot:{r['id']}", ok, {"source": ctx.conn.id})
        out.append(ActionResult(r["id"], ok, f"{r['name']}: {message}"))
    return out


def _audit(actor: str, action: str, target: str, ok: bool, details: dict[str, Any]) -> None:
    """One audit entry for a source page action."""
    from orchestrator.audit import log as audit
    from orchestrator.db.session import session_scope
    from orchestrator.domain.enums import AuditResult

    with session_scope() as session:
        audit.record(
            session,
            actor=actor,
            action=action,
            target=target,
            result=AuditResult.OK if ok else AuditResult.ERROR,
            details=details,
        )


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
        Resource(
            "snapshots",
            "Snapshots",
            (
                Column("name", "Snapshot", "mono"),
                Column("datastore", "Datastore"),
                Column("taken", "Taken", "ago"),
                Column("size", "Size", "bytes"),
                Column("state", "Verified", "pill"),
                Column("protected", "Protected"),
            ),
            _pbs_snapshots,
            chips=(
                Chip("unverified", "Not verified"),
                Chip("verified", "Verified"),
                Chip("protected", "Protected"),
            ),
            actions=(
                Action("verify", "Verify", bulk=True),
                Action("protect", "Protect", bulk=True),
                Action("unprotect", "Unprotect", bulk=True, confirm=True),
            ),
            act=_pbs_act,
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

    def about(self, ctx: SourceCtx, target: str, details: dict[str, Any]) -> bool:
        return Connector.about(self, ctx, target, details) or (
            target.startswith("snapshot:") and details.get("source") == ctx.conn.id
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

    async def overview(self, ctx: SourceCtx) -> Overview:
        ov = Overview(facts=self.facts(ctx))
        if ctx.adapter is None:
            return ov
        stores = await _pbs_datastores(ctx)
        snaps = await _pbs_snapshots(ctx)
        unverified = [x for x in snaps if "unverified" in x["chips"]]
        ov.tiles = [
            {"label": "Datastores", "value": len(stores)},
            {"label": "Snapshots", "value": len(snaps), "sub": f"{len(unverified)} not verified"},
        ]
        if snaps:
            ov.tiles.append(
                {
                    "label": "Newest",
                    "value": snaps[0]["group"],
                    "sub": snaps[0]["taken"][:16].replace("T", " "),
                }
            )
        if stores:
            worst = max(stores, key=lambda x: x["pct"] or 0)
            tone = (
                "fail"
                if (worst["pct"] or 0) >= 95
                else "warn" if (worst["pct"] or 0) >= 85 else None
            )
            ov.tiles.append(
                {
                    "label": "Fullest datastore",
                    "value": f"{worst['pct'] or 0}%",
                    "sub": worst["name"],
                    "tone": tone,
                }
            )
            if tone:
                ov.attention.append(
                    {
                        "tone": tone,
                        "label": f"{worst['pct']}% full",
                        "text": f"datastore {worst['name']} is filling up",
                        "href": "?tab=datastores",
                        "action": "Open",
                    }
                )
        if unverified:
            ov.attention.append(
                {
                    "tone": "warn",
                    "label": "not verified",
                    "text": f"{_count(len(unverified), 'snapshot')} not verified yet",
                    "href": "?tab=snapshots",
                    "action": "Open snapshots",
                }
            )
        return ov


# -- Portainer ------------------------------------------------------------------------------------


async def _pt_environments(ctx: SourceCtx) -> list[dict[str, Any]]:
    envs = await ctx.adapter.list_environments()
    runs_on = _pt_runs_on(ctx)
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
            "links": runs_on.get(e.id, []),
        }
        for e in envs
    ]


def _pt_runs_on(ctx: SourceCtx) -> dict[int, list[dict[str, str]]]:
    """Environment -> where it runs: the service its linked stacks live under (the root of
    their tree) and that service's Proxmox guest, each a link to its own page."""
    from sqlmodel import Session, select

    from orchestrator.connectors.ownership import node_href
    from orchestrator.db.models import GuestKind, Node, Service
    from orchestrator.db.session import get_engine
    from orchestrator.registry.tree import TreeIndex

    slugs: dict[int, set[str]] = {}
    for (_kind, eid, _name), linked in _pt_links(ctx).items():
        slugs.setdefault(eid, set()).update(linked)
    out: dict[int, list[dict[str, str]]] = {}
    with Session(get_engine()) as session:
        services = list(session.exec(select(Service)).all())
        tree = TreeIndex(services)
        by_slug = {s.slug: s for s in services}
        for eid, linked in slugs.items():
            roots = {
                tree.root(by_slug[x]).slug: tree.root(by_slug[x]) for x in linked if x in by_slug
            }
            links: list[dict[str, str]] = []
            for root in sorted(roots.values(), key=lambda r: r.slug):
                if root.slug in linked:
                    continue  # the stack's own service, not where it runs
                links.append(
                    {
                        "label": "runs on " + (root.name or root.slug),
                        "href": f"/services/{root.slug}",
                    }
                )
                node = session.get(Node, root.node_id) if root.node_id is not None else None
                if node is not None and root.guest_kind is not GuestKind.NONE:
                    links.append(
                        {
                            "label": f"{node.name} · {root.guest_kind.value} {root.guest_id}",
                            "href": node_href(node, tab="guests", find=str(root.guest_id)),
                        }
                    )
            if links:
                out[eid] = links
    return out


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

    def about(self, ctx: SourceCtx, target: str, details: dict[str, Any]) -> bool:
        if Connector.about(self, ctx, target, details):
            return True
        if not target.startswith("portainer:"):
            return False
        default = getattr(ctx.adapters, "default_portainer", "") if ctx.adapters else ""
        return (details.get("instance") or default) == ctx.conn.id

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


def _owned_proxy_hosts(ctx: SourceCtx) -> list[Any]:
    """This proxy's hosts as kept here (unowned ones are the default proxy's)."""
    from sqlmodel import Session

    from orchestrator.connectors.ownership import owned_by
    from orchestrator.db.models import ProxyHost
    from orchestrator.db.session import get_engine

    default = getattr(ctx.adapters, "default_proxy", "") if ctx.adapters else ""
    with Session(get_engine()) as session:
        hosts = owned_by(session, ProxyHost, ctx.conn.id, default=ctx.conn.id == default)
    return sorted(hosts, key=lambda h: h.hostname)


async def _proxy_hosts(ctx: SourceCtx) -> list[dict[str, Any]]:
    """The tab's own module reads them with Traefik's view; this is the plain list."""
    return [
        {
            "id": h.id,
            "name": h.hostname,
            "upstream": h.upstream,
            "state": "on" if h.enabled else "off",
            "chips": [] if h.service_id else ["unlinked"],
        }
        for h in _owned_proxy_hosts(ctx)
    ]


async def _proxy_middlewares(ctx: SourceCtx) -> list[dict[str, Any]]:
    names = await ctx.adapter.list_middlewares() or []
    routes = await ctx.adapter.list_routes() or []
    rows = []
    for name in sorted(names):
        users = sorted(
            h
            for r in routes
            if name in (getattr(r, "middlewares", None) or ())
            for h in r.hostnames
        )
        rows.append(
            {
                "id": name,
                "name": name,
                "used": len(users),
                "hosts": ", ".join(users[:4])
                + (f" and {len(users) - 4} more" if len(users) > 4 else ""),
                "chips": [] if users else ["unused"],
            }
        )
    return rows


class ProxyConnector(Connector):
    kind = "proxy"
    title = "Traefik Manager"
    group = "Proxy"
    family = "network"
    blurb = "proxy hosts, routes and middlewares"
    scripts = ("objects.js", "format_routes.js", "source_proxy.js")
    resources = (
        Resource(
            "hosts",
            "Proxy hosts",
            (
                Column("name", "Hostname", "mono"),
                Column("upstream", "Points to", "mono"),
                Column("state", "On", "pill"),
            ),
            _proxy_hosts,
            chips=(Chip("unlinked", "Not linked"),),
            view="proxy.hosts",
        ),
        Resource(
            "middlewares",
            "Middlewares",
            (
                Column("name", "Middleware", "mono"),
                Column("used", "Proxy hosts", "num"),
                Column("hosts", "Used by"),
            ),
            _proxy_middlewares,
            chips=(Chip("unused", "Unused"),),
        ),
    )
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

    def tabs(self) -> list[Tab]:
        """The SourceProxy wireframe: its lists, then Import and Format routes."""
        tabs = super().tabs()
        at = next(i for i, t in enumerate(tabs) if t.key == "settings")
        return [
            *tabs[:at],
            Tab("import", "Import", "proxy.import"),
            Tab("format", "Format routes", "proxy.format"),
            *tabs[at:],
        ]

    def about(self, ctx: SourceCtx, target: str, details: dict[str, Any]) -> bool:
        if Connector.about(self, ctx, target, details):
            return True
        kind, _, host = target.partition(":")
        return kind == "proxy_host" and host in {h.hostname for h in _owned_proxy_hosts(ctx)}

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


async def _power_targets(ctx: SourceCtx) -> list[dict[str, Any]]:
    """The nodes with a power target, with what this power manager says about each."""
    from sqlmodel import Session, select

    from orchestrator.db.models import Node
    from orchestrator.db.session import get_engine

    with Session(get_engine()) as session:
        nodes = [
            n
            for n in session.exec(select(Node).order_by(Node.name)).all()
            if n.power_mgr_target.strip()
        ]
    check = await ctx.adapter.check([n.power_mgr_target for n in nodes])
    by_target = {t.target: t for t in check.targets}
    rows = []
    for n in nodes:
        t = by_target.get(n.power_mgr_target)
        if t is not None and t.state is not None:
            state = t.state.value
        elif t is not None and t.online is not None:
            state = "on" if t.online else "off"  # it only said whether it is online
        else:
            state = "unknown"
        rows.append(
            {
                "id": n.name,
                "name": n.name,
                "target": n.power_mgr_target,
                "state": state,
                "detail": (t.detail if t is not None else "") or "",
                "always_on": "always on" if n.always_on else "",
                "chips": [state if state in ("on", "off") else "unknown"]
                + (["always_on"] if n.always_on else []),
            }
        )
    return rows


async def _power_act(
    ctx: SourceCtx, action: str, rows: list[dict[str, Any]], actor: str
) -> list[ActionResult]:
    """Wake, restart or power off nodes through this power manager, each audited."""
    out = []
    reason = f"manual dashboard action by {actor}"
    for r in rows:
        try:
            if action == "wake":
                await ctx.adapter.wake(r["target"], reason=reason)
            elif action == "restart":
                await ctx.adapter.restart(r["target"], reason=reason)
            else:
                await ctx.adapter.power_off(r["target"], reason=reason)
            ok, message = True, f"{action.replace('_', ' ')} sent"
        except Exception as exc:
            ok, message = False, f"{type(exc).__name__}: {exc}"[:200]
        _audit(
            actor,
            "node.power_action",
            f"node:{r['name']}",
            ok,
            {"action": action, "source": ctx.conn.id},
        )
        out.append(ActionResult(r["id"], ok, f"{r['name']}: {message}"))
    return out


class PowerConnector(Connector):
    kind = "power"
    title = "Power manager (MQTT)"
    group = "Power"
    family = "network"
    blurb = "wake and power off nodes"
    resources = (
        Resource(
            "targets",
            "Power targets",
            (
                Column("name", "Node"),
                Column("target", "Target", "mono"),
                Column("state", "Power", "pill"),
                Column("always_on", "Always on"),
                Column("detail", "Says"),
            ),
            _power_targets,
            chips=(Chip("on", "On"), Chip("off", "Off"), Chip("unknown", "Unknown")),
            actions=(
                Action("wake", "Wake", bulk=True),
                Action("restart", "Restart…", danger=True, confirm=True, typed=True),
                Action("power_off", "Power off…", danger=True, confirm=True, typed=True),
            ),
            act=_power_act,
        ),
    )

    def about(self, ctx: SourceCtx, target: str, details: dict[str, Any]) -> bool:
        return Connector.about(self, ctx, target, details) or details.get("source") == ctx.conn.id

    async def summary(self, ctx: SourceCtx) -> Summary:
        summary = await Connector.summary(self, ctx)
        if ctx.adapter is None or summary.state == "fail":
            return summary
        try:
            rows = await _power_targets(ctx)
            on = sum(1 for r in rows if r["state"] == "on")
            summary.has = f"{_count(len(rows), 'power target')} · {on} on"
        except Exception as exc:
            summary.detail = str(exc)[:200]
        return summary

    async def overview(self, ctx: SourceCtx) -> Overview:
        ov = Overview(facts=self.facts(ctx))
        if ctx.adapter is None:
            return ov
        rows = await _power_targets(ctx)
        on = sum(1 for r in rows if r["state"] == "on")
        off = sum(1 for r in rows if r["state"] == "off")
        ov.tiles = [
            {"label": "Targets", "value": len(rows)},
            {"label": "On", "value": on, "of": len(rows)},
            {"label": "Off", "value": off},
        ]
        for r in rows:
            if r["state"] == "unknown":
                ov.attention.append(
                    {
                        "tone": "off",
                        "label": "unknown",
                        "text": f"{r['name']}: the power manager says nothing about {r['target']}",
                    }
                )
        return ov


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
