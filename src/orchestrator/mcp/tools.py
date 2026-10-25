"""The tools an assistant can call.

Each tool names the scope it needs and how risky a call is:

* ``read``       looks, changes nothing
* ``act``        changes something, runs at once, is logged (and audited like any action)
* ``dangerous``  waits for a person to approve it (``mcp/approvals.py``); needs the ``propose``
                 scope as well, so a token that was never meant to ask for these cannot

The tools call the same code the dashboard's buttons do (the route functions), so every guard
those have - mid-backup refusals, deploy guards, the one switch - applies here unchanged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, col, desc, select

from orchestrator import __version__
from orchestrator.config import Settings
from orchestrator.db.models import (
    McpGrant,
    Node,
    PendingApproval,
    PipelineRun,
    Service,
    ServiceState,
)
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import ApprovalKind, ApprovalStatus, HealthState, PipelineKind
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

READ, ACT, DANGEROUS = "read", "act", "dangerous"


class ToolError(Exception):
    """A call that cannot be carried out; the message goes back to the assistant."""


@dataclass
class ToolContext:
    session: Session
    adapters: Any
    settings: Settings
    #: Who the audit log and the run history name: ``mcp:<connection label>``.
    actor: str
    grant: McpGrant | None = None

    @property
    def admin(self) -> Any:
        """Stands in for the logged-in admin the route functions expect (they read the name)."""
        return SimpleNamespace(username=self.actor)


Runner = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    scope: str
    run: Runner
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    risk: str | Callable[[dict[str, Any]], str] = READ
    #: One line for the approval page: what happens if a person says yes.
    consequence: Callable[[dict[str, Any]], str] | None = None

    def risk_of(self, args: dict[str, Any]) -> str:
        return self.risk(args) if callable(self.risk) else self.risk

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.properties,
            "required": list(self.required),
            "additionalProperties": False,
        }

    @property
    def possibly_dangerous(self) -> bool:
        return callable(self.risk) or self.risk == DANGEROUS

    @property
    def writes(self) -> bool:
        return callable(self.risk) or self.risk != READ

    def listing(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": not self.writes,
                "destructiveHint": self.possibly_dangerous,
                "idempotentHint": not self.writes,
                "openWorldHint": False,
            },
        }


# -- arguments ---------------------------------------------------------------------------------

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "boolean": (bool,),
}


def validate(tool: Tool, args: Any) -> dict[str, Any]:
    """The arguments, or a ToolError saying what is wrong. No unknown keys, no wrong types."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    for key in tool.required:
        if key not in args:
            raise ToolError(f"missing argument: {key}")
    out: dict[str, Any] = {}
    for key, value in args.items():
        spec = tool.properties.get(key)
        if spec is None:
            raise ToolError(f"unknown argument: {key}")
        kind = spec.get("type", "string")
        ok = isinstance(value, _TYPES[kind]) and not (kind == "integer" and isinstance(value, bool))
        if not ok:
            raise ToolError(f"{key} must be a {kind}")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(f"{key} must be one of: {', '.join(map(str, spec['enum']))}")
        if kind == "integer":
            if "minimum" in spec and value < spec["minimum"]:
                raise ToolError(f"{key} must be at least {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                raise ToolError(f"{key} must be at most {spec['maximum']}")
        out[key] = value
    return out


def _s(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _i(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


def _b(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


SLUG = _s("The service's slug, as list_services shows it")


async def _call(coro: Awaitable[Any]) -> Any:
    """Run a route function; its HTTP errors become the message the assistant reads."""
    try:
        return await coro
    except HTTPException as exc:
        raise ToolError(f"{exc.status_code}: {exc.detail}") from exc


def _limit(a: dict[str, Any], default: int = 20, top: int = 100) -> int:
    return max(1, min(int(a.get("limit", default)), top))


# -- read --------------------------------------------------------------------------------------


async def _list_services(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import dashboard

    rows = await dashboard.list_services(ctx.session)
    states = {s.service_id: s for s in ctx.session.exec(select(ServiceState)).all()}
    ids = {s.slug: s.id for s in ctx.session.exec(select(Service)).all()}
    out = []
    for row in rows:
        if a.get("group") and row["group"] != a["group"]:
            continue
        sid = ids.get(str(row["slug"]))
        st = states.get(sid) if sid is not None else None
        out.append(
            {
                **{k: row[k] for k in ("slug", "name", "enabled", "parent", "group", "node")},
                "guest": f"{row['guest_kind']}/{row['guest_id']}" if row["guest_id"] else None,
                "ip": row["effective_ip"] or None,
                "health": st.last_verdict.value if st else HealthState.UNKNOWN.value,
                "health_at": st.last_verdict_at.isoformat() if st and st.last_verdict_at else None,
            }
        )
    if a.get("health"):
        out = [r for r in out if r["health"] == a["health"]]
    return out


async def _get_service(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import dashboard

    return await _call(dashboard.get_service(a["slug"], ctx.session))


async def _health_history(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import dashboard

    return await _call(
        dashboard.get_service_health_history(a["slug"], ctx.session, _limit(a, 20, 200))
    )


async def _linked_state(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import linked

    if a.get("slug"):
        return await _call(linked.linked_state(a["slug"], ctx.session, ctx.adapters, ctx.settings))
    return await _call(linked.linked_all(ctx.session, ctx.adapters, ctx.settings))


async def _activity(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers.activity import KINDS, build_feed

    kind = a.get("kind")
    if kind and kind not in KINDS:
        raise ToolError(f"kind must be one of: {', '.join(KINDS)}")
    return build_feed(
        ctx.session,
        service=a.get("service"),
        kind=kind,
        problems=bool(a.get("problems", False)),
        since_hours=a.get("since_hours"),
        q=a.get("q"),
        limit=_limit(a, 30, 200),
    )


async def _list_backups(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import backup

    return await backup.list_backups(ctx.session, a.get("slug"), _limit(a, 20, 100))


async def _backup_plan(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import backup

    return await _call(backup.backup_plan(ctx.session, a["slug"], a.get("policy")))


async def _list_runs(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import backup

    runs = await backup.list_runs(ctx.session, _limit(a, 20, 100), a.get("slug"), None)
    kind = a.get("kind")
    if kind:
        runs = [r for r in runs if r["kind"] == kind]
    return [{k: v for k, v in r.items() if k != "steps"} for r in runs]


async def _get_run(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import backup

    return await _call(backup.get_run(a["run_id"], ctx.session))


async def _deploy_history(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import deploys

    return await _call(deploys.deploy_history(a["slug"], ctx.session, _limit(a, 10, 50)))


async def _infra_status(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import infra

    return await infra.infra_status(ctx.adapters, ctx.settings, ctx.session)


async def _nodes(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import infra

    return await infra.nodes_status(ctx.session, ctx.adapters)


async def _guests(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import infra

    return await _call(infra.list_guests(ctx.adapters, ctx.session, a.get("node")))


async def _proxy_hosts(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import proxy_hosts

    rows = await proxy_hosts.list_proxy_hosts(ctx.session, ctx.settings, ctx.adapters)
    keep = ("hostname", "upstream", "enabled", "router_provider", "service", "sync", "state")
    return [{k: r[k] for k in keep if k in r} for r in rows]


async def _portainer_stacks(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import portainer

    return await _call(portainer.stacks(ctx.adapters, ctx.session, a.get("endpoint_id")))


async def _portainer_containers(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import portainer

    return await _call(
        portainer.containers(
            a["endpoint_id"], ctx.adapters, ctx.session, a.get("stack"), not a.get("running_only")
        )
    )


async def _container_logs(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import portainer

    out = await _call(
        portainer.container_logs(
            a["endpoint_id"], a["container"], ctx.adapters, _limit(a, 100, 1000), None, True
        )
    )
    return out["lines"]


async def _list_dumps(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import dumps

    if a.get("slug"):
        out = await _call(dumps.get_dump(a["slug"], ctx.session, ctx.settings))
        out.pop("restore_hint", None)
        return out
    return await dumps.all_dumps(ctx.session)


async def _status(ctx: ToolContext, a: dict[str, Any]) -> Any:
    states = ctx.session.exec(select(ServiceState)).all()
    bad = [s for s in states if s.last_verdict is not HealthState.HEALTHY]
    slugs = {s.id: s.slug for s in ctx.session.exec(select(Service)).all()}
    waiting = ctx.session.exec(
        select(PendingApproval).where(
            PendingApproval.kind == ApprovalKind.MCP_ACTION,
            PendingApproval.status == ApprovalStatus.PENDING,
        )
    ).all()
    return {
        "version": __version__,
        "dry_run": ctx.settings.dry_run,
        "services": len(slugs),
        "not_healthy": sorted(
            f"{slugs.get(s.service_id, s.service_id)}={s.last_verdict.value}" for s in bad
        ),
        "nodes": len(ctx.session.exec(select(Node)).all()),
        "requests_waiting_for_a_person": len(waiting),
        "you": {
            "connection": ctx.grant.label if ctx.grant else None,
            "scopes": list(ctx.grant.scopes) if ctx.grant else [],
            "ask_before_every_change": bool(ctx.grant and ctx.grant.confirm_writes),
        },
    }


async def _get_request(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.mcp import approvals

    return approvals.request_view(ctx.session, ctx.settings, a["request_id"], ctx.grant)


# -- act ---------------------------------------------------------------------------------------

_BACKGROUND: set[asyncio.Task[Any]] = set()


async def _run_backup(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import backup

    slug = a["slug"]
    if ctx.session.exec(select(Service).where(Service.slug == slug)).first() is None:
        raise ToolError(f"no service {slug!r}")
    adapters, settings, actor = ctx.adapters, ctx.settings, ctx.actor
    verify, wake = a.get("verify", True), a.get("wake", True)

    async def go() -> dict[str, Any]:
        with Session(get_engine()) as s:
            try:
                return await backup.run_backup(
                    s, adapters, settings, slug, actor=actor, verify=verify, wake=wake
                )
            except HTTPException as exc:
                return {"status": "failed", "error": f"{exc.status_code}: {exc.detail}"}
            except Exception as exc:  # the run row records it; do not lose the task silently
                log.exception("mcp.backup.crashed", service=slug)
                return {"status": "failed", "error": str(exc)}

    task = asyncio.create_task(go(), name=f"mcp-backup:{slug}")
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    done, _ = await asyncio.wait({task}, timeout=3.0)
    if done:
        result = task.result()
        if result.get("status") == "failed" and result.get("error") and "id" not in result:
            raise ToolError(str(result["error"]))
        return {k: v for k, v in result.items() if k != "steps"}
    latest = ctx.session.exec(
        select(PipelineRun)
        .where(
            PipelineRun.kind == PipelineKind.BACKUP,
            col(PipelineRun.service_id).in_(select(Service.id).where(Service.slug == slug)),
        )
        .order_by(desc(PipelineRun.started_at))
        .limit(1)
    ).first()
    return {
        "started": True,
        "service": slug,
        "run_id": latest.id if latest else None,
        "next": "A backup takes a while (it may wake a node first). Poll get_run(run_id) or "
        f"list_runs(slug='{slug}', kind='backup').",
    }


async def _run_dump(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import dumps

    out = await _call(dumps.run_dump(a["slug"], ctx.session, ctx.adapters, ctx.settings, ctx.admin))
    out["next"] = "A dump takes seconds to minutes. Poll get_run(run_id)."
    return {k: v for k, v in out.items() if k != "steps"}


async def _deploy(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import deploys

    body = deploys.DeployIn(
        guards=not a.get("skip_guards", False),
        guards_only=a.get("guards_only", False),
        reason=a.get("reason", ""),
    )
    out = await _call(
        deploys.deploy(a["slug"], body, ctx.session, ctx.adapters, ctx.settings, ctx.admin)
    )
    out["next"] = "A deploy takes minutes. Poll get_run(run_id) or deploy_history(slug)."
    return out


async def _wake(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import wake

    out = wake.start_wake(ctx.session, ctx.adapters, ctx.settings, a["slug"], actor=ctx.actor)
    out["next"] = "Waking takes a while. Poll get_run(run_id)."
    return {k: v for k, v in out.items() if k != "steps"}


async def _node_power(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import infra

    return await _call(
        infra.apply_node_power(
            ctx.session,
            ctx.adapters,
            ctx.actor,
            a["node"],
            a["action"],
            force=a.get("force", False),
        )
    )


async def _switch(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import linked

    body = linked.SwitchIn(
        state=a["state"],
        include_guest=a.get("include_guest", False),
        restore_children=a.get("restore_children", False),
    )
    return await _call(
        linked.switch(a["slug"], body, ctx.session, ctx.adapters, ctx.settings, ctx.admin)
    )


async def _service_action(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import actions

    message = await _call(
        actions.do_service_action(
            ctx.session,
            ctx.adapters,
            ctx.settings,
            ctx.actor,
            a["slug"],
            a["action"],
            target=a.get("target", "service"),
        )
    )
    return {"slug": a["slug"], "action": a["action"], "message": message}


async def _stack_action(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import stacks

    return await _call(
        stacks.stack_action(
            a["endpoint_id"], a["stack"], a["action"], ctx.session, ctx.adapters, ctx.admin
        )
    )


async def _container_action(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import stacks

    return await _call(
        stacks.container_action(
            a["endpoint_id"], a["container"], a["action"], ctx.session, ctx.adapters, ctx.admin
        )
    )


async def _propose_restore(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import approvals

    out = await _call(approvals.propose(a["slug"], ctx.session, ctx.settings, ctx.admin))
    out["next"] = "Nothing happens until a person approves the restore at the link in 'url'."
    return out


# -- dangerous ---------------------------------------------------------------------------------


async def _guest_power(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import infra

    body = infra.GuestPowerIn(action=a["action"])
    return await _call(
        infra.guest_power_action(a["node"], a["vmid"], body, ctx.session, ctx.adapters, ctx.admin)
    )


async def _delete_stack(ctx: ToolContext, a: dict[str, Any]) -> Any:
    from orchestrator.web.routers import stacks

    body = stacks.DeleteIn(
        confirm_name=a["confirm_name"],
        take_down=a.get("take_down", False),
        remove_services=a.get("remove_services", True),
    )
    return await _call(
        stacks.delete(
            a["endpoint_id"], a["stack"], body, ctx.session, ctx.adapters, ctx.settings, ctx.admin
        )
    )


def _deploy_risk(a: dict[str, Any]) -> str:
    return DANGEROUS if a.get("skip_guards") else ACT


def _node_risk(a: dict[str, Any]) -> str:
    return ACT if a.get("action") == "wake" else DANGEROUS


def _switch_risk(a: dict[str, Any]) -> str:
    return DANGEROUS if a.get("state") == "off" and a.get("include_guest") else ACT


def _service_action_risk(a: dict[str, Any]) -> str:
    guest = a.get("target") == "guest"
    return DANGEROUS if guest and a.get("action") in {"stop", "restart"} else ACT


def _guest_risk(a: dict[str, Any]) -> str:
    return DANGEROUS if a.get("action") == "stop" else ACT


def _slug_list(a: dict[str, Any]) -> str:
    return str(a.get("slug") or a.get("node") or "")


_ACTIONS = ["start", "stop", "restart"]

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        # -- read
        Tool(
            "orchestrator_status",
            "Orchestrator status",
            "A one-call overview: version, dry-run mode, how many services and nodes, which "
            "services are not healthy, requests waiting for a person, and what this connection "
            "is allowed to do. Start here.",
            "read",
            _status,
        ),
        Tool(
            "list_services",
            "List services",
            "Every registered service with its parent, group, node, guest, IP and last health "
            "verdict (healthy / failed / unknown / down). Optionally only one group or verdict.",
            "read",
            _list_services,
            {
                "group": _s("Only services in this group"),
                "health": _s("Only this verdict", enum=[h.value for h in HealthState]),
            },
        ),
        Tool(
            "get_service",
            "Get a service",
            "Everything about one service: configuration, probes and their latest results, "
            "backup policy, controller, children, deploy settings.",
            "read",
            _get_service,
            {"slug": SLUG},
            ("slug",),
        ),
        Tool(
            "service_health_history",
            "Service health history",
            "The last health verdicts for a service, oldest first, with the reason for each.",
            "read",
            _health_history,
            {"slug": SLUG, "limit": _i("How many (default 20, max 200)", minimum=1, maximum=200)},
            ("slug",),
        ),
        Tool(
            "linked_state",
            "Linked state",
            "What is linked to a service (proxy hosts, Portainer stack or container, Proxmox "
            "guest), each as it is right now, and whether they agree. Without a slug: every "
            "service that has something linked.",
            "read",
            _linked_state,
            {"slug": SLUG},
        ),
        Tool(
            "recent_activity",
            "Recent activity",
            "The activity feed, newest first: wakes, backups, restores, switches, deploys, "
            "admin actions and alerts. Use problems=true to see only what failed.",
            "read",
            _activity,
            {
                "service": _s("Only this service"),
                "kind": _s("wake | backup | restore | switch | deploy | admin | alert"),
                "problems": _b("Only what failed"),
                "since_hours": _i("Only the last N hours", minimum=1, maximum=24 * 365),
                "q": _s("Text to look for"),
                "limit": _i("How many (default 30, max 200)", minimum=1, maximum=200),
            },
        ),
        Tool(
            "list_backups",
            "List backups",
            "Backup records (PBS snapshots), newest first, with size and whether they verified.",
            "read",
            _list_backups,
            {"slug": SLUG, "limit": _i("How many (default 20)", minimum=1, maximum=100)},
        ),
        Tool(
            "backup_plan",
            "Backup plan",
            "What a backup of a service would wake and in which order, and what could never "
            "be woken. Changes nothing.",
            "read",
            _backup_plan,
            {"slug": SLUG, "policy": _s("Preview under this backup policy instead")},
            ("slug",),
        ),
        Tool(
            "list_runs",
            "List pipeline runs",
            "Recent pipeline runs (backup, wake, restore, switch, deploy), newest first. "
            "Use get_run for the steps of one.",
            "read",
            _list_runs,
            {
                "slug": SLUG,
                "kind": _s("backup | wake | restore | switch | deploy"),
                "limit": _i("How many (default 20)", minimum=1, maximum=100),
            },
        ),
        Tool(
            "get_run",
            "Get a pipeline run",
            "One pipeline run with all its steps and any error. Poll this after starting a "
            "backup, deploy or wake.",
            "read",
            _get_run,
            {"run_id": _i("The run id", minimum=1)},
            ("run_id",),
        ),
        Tool(
            "deploy_history",
            "Deploy history",
            "A service's recent deploys with their steps and results.",
            "read",
            _deploy_history,
            {"slug": SLUG, "limit": _i("How many (default 10)", minimum=1, maximum=50)},
            ("slug",),
        ),
        Tool(
            "infra_status",
            "Infrastructure status",
            "Can the orchestrator reach Proxmox, PBS, the power manager, Portainer and every "
            "node? One check per system.",
            "read",
            _infra_status,
        ),
        Tool(
            "list_nodes",
            "List nodes",
            "The nodes with their live power state (power manager and Proxmox).",
            "read",
            _nodes,
        ),
        Tool(
            "list_guests",
            "List Proxmox guests",
            "Proxmox VMs and containers with their status and the service each one runs.",
            "read",
            _guests,
            {"node": _s("Only this node")},
        ),
        Tool(
            "list_proxy_hosts",
            "List proxy hosts",
            "The reverse-proxy hosts: hostname, upstream, enabled, which service, and whether "
            "the router agrees.",
            "read",
            _proxy_hosts,
        ),
        Tool(
            "portainer_stacks",
            "Portainer stacks",
            "Portainer stacks with their containers and which service each is linked to.",
            "read",
            _portainer_stacks,
            {"endpoint_id": _i("Only this Portainer environment", minimum=1)},
        ),
        Tool(
            "portainer_containers",
            "Portainer containers",
            "Containers on a Portainer environment with their state and health.",
            "read",
            _portainer_containers,
            {
                "endpoint_id": _i("The Portainer environment id", minimum=1),
                "stack": _s("Only this stack's containers"),
                "running_only": _b("Leave out stopped containers"),
            },
            ("endpoint_id",),
        ),
        Tool(
            "container_logs",
            "Container logs",
            "The last log lines of a container. Secrets are redacted.",
            "read",
            _container_logs,
            {
                "endpoint_id": _i("The Portainer environment id", minimum=1),
                "container": _s("Container name or id"),
                "limit": _i("Lines (default 100, max 1000)", minimum=1, maximum=1000),
            },
            ("endpoint_id", "container"),
        ),
        Tool(
            "list_dumps",
            "List dumps",
            "The database dumps kept for services that are dumped on their own: how many, how "
            "big, the newest good one, the schedule. With a slug: that service's files and last run.",
            "read",
            _list_dumps,
            {"slug": SLUG},
        ),
        Tool(
            "get_request",
            "Check a request",
            "The state of a dangerous request you made earlier (waiting / approved / denied / "
            "expired) and, once a person approved it, what happened.",
            "read",
            _get_request,
            {"request_id": _s("The request_id you were given")},
            ("request_id",),
        ),
        # -- act
        Tool(
            "run_backup",
            "Run a backup",
            "Start a backup of a service now (wakes what it needs first, dumps, verifies). It "
            "usually takes minutes: you get a run id back; poll get_run. Refused when the "
            "service is excluded from backups or one is already running.",
            "backup",
            _run_backup,
            {
                "slug": SLUG,
                "verify": _b("Run a PBS verify after the dump (default true)"),
                "wake": _b("Wake prerequisites and the target first (default true)"),
            },
            ("slug",),
            ACT,
        ),
        Tool(
            "run_dump",
            "Dump a database",
            "Dump one service's own database now (its dump tool runs inside its container, the "
            "output is compressed, checked and kept). Returns a run id; poll get_run. Refused "
            "when the service has no dump configured, its node or container is off, or one is running.",
            "backup",
            _run_dump,
            {"slug": SLUG},
            ("slug",),
            ACT,
        ),
        Tool(
            "propose_restore",
            "Propose a restore",
            "Ask for a service's guest to be restored from its latest verified backup. This "
            "only creates a proposal and alerts the admins; nothing is restored until a "
            "person approves it.",
            "backup",
            _propose_restore,
            {"slug": SLUG},
            ("slug",),
            ACT,
        ),
        Tool(
            "deploy_service",
            "Deploy a service",
            "Redeploy a service through its configured deploy method, with its pre-guards "
            "(health, fresh backup, ...) and post-checks. guards_only=true only runs the "
            "guards and reports. skip_guards=true bypasses them and needs a written reason "
            "and a person's approval.",
            "deploy",
            _deploy,
            {
                "slug": SLUG,
                "guards_only": _b("Run the pre-guards and report; change nothing"),
                "skip_guards": _b("Bypass the guards (needs reason; a person must approve)"),
                "reason": _s("Why, for an unguarded deploy (audited)"),
            },
            ("slug",),
            _deploy_risk,
            lambda a: f"Deploy {a.get('slug')} WITHOUT its guards. Reason: {a.get('reason') or '-'}",
        ),
        Tool(
            "wake_service",
            "Wake a service",
            "Power on what a service needs (node, guest) and wait until it is healthy. Returns "
            "a run id; poll get_run.",
            "wake",
            _wake,
            {"slug": SLUG},
            ("slug",),
            ACT,
        ),
        Tool(
            "node_power",
            "Node power",
            "Wake a node (acts at once). power_off and restart cut or cycle the power of a "
            "whole host and need a person's approval.",
            "wake",
            _node_power,
            {
                "node": _s("Node name"),
                "action": _s("wake | power_off | restart", enum=["wake", "power_off", "restart"]),
                "force": _b("power_off / restart: skip the graceful path"),
            },
            ("node", "action"),
            _node_risk,
            lambda a: f"{a.get('action')} node {a.get('node')}"
            + (" (forced)" if a.get("force") else ""),
        ),
        Tool(
            "switch_service",
            "Switch a service on or off",
            "The one switch: turn a service and everything linked to it (proxy hosts, stack, "
            "children) on or off in the right order. include_guest=true also stops the "
            "Proxmox guest and needs a person's approval.",
            "control",
            _switch,
            {
                "slug": SLUG,
                "state": _s("on | off", enum=["on", "off"]),
                "include_guest": _b("Off only: also stop the Proxmox guest (needs approval)"),
                "restore_children": _b("On only: also start children that were on before"),
            },
            ("slug", "state"),
            _switch_risk,
            lambda a: f"Switch {a.get('slug')} off and stop its Proxmox guest",
        ),
        Tool(
            "service_action",
            "Start, stop or restart a service",
            "Act on one service through its controller (Portainer container, add-on, systemd "
            "unit). target=guest acts on its Proxmox guest; stopping or restarting a guest "
            "needs a person's approval. Refused while a backup of the tree is running.",
            "control",
            _service_action,
            {
                "slug": SLUG,
                "action": _s("start | stop | restart", enum=_ACTIONS),
                "target": _s("service (default) | guest", enum=["service", "guest"]),
            },
            ("slug", "action"),
            _service_action_risk,
            lambda a: f"{a.get('action')} the Proxmox guest of {a.get('slug')}",
        ),
        Tool(
            "stack_action",
            "Start, stop or restart a stack",
            "Start, stop or restart a Portainer stack.",
            "control",
            _stack_action,
            {
                "endpoint_id": _i("The Portainer environment id", minimum=1),
                "stack": _s("Stack name"),
                "action": _s("start | stop | restart", enum=_ACTIONS),
            },
            ("endpoint_id", "stack", "action"),
            ACT,
        ),
        Tool(
            "container_action",
            "Start, stop or restart a container",
            "Start, stop or restart one Portainer container.",
            "control",
            _container_action,
            {
                "endpoint_id": _i("The Portainer environment id", minimum=1),
                "container": _s("Container name or id"),
                "action": _s("start | stop | restart", enum=_ACTIONS),
            },
            ("endpoint_id", "container", "action"),
            ACT,
        ),
        # -- dangerous
        Tool(
            "guest_power",
            "Proxmox guest power",
            "Start (acts at once) or stop (needs a person's approval) any Proxmox VM or "
            "container.",
            "control",
            _guest_power,
            {
                "node": _s("Proxmox node"),
                "vmid": _i("Guest id", minimum=1),
                "action": _s("start | stop", enum=["start", "stop"]),
            },
            ("node", "vmid", "action"),
            _guest_risk,
            lambda a: f"{a.get('action')} guest {a.get('vmid')} on {a.get('node')}",
        ),
        Tool(
            "delete_stack",
            "Delete a Portainer stack",
            "Delete a Portainer stack, optionally tearing it down first and removing the "
            "services registered for it. Always needs a person's approval.",
            "control",
            _delete_stack,
            {
                "endpoint_id": _i("The Portainer environment id", minimum=1),
                "stack": _s("Stack name"),
                "confirm_name": _s("The stack name again, to confirm"),
                "take_down": _b("Stop and remove its containers first"),
                "remove_services": _b("Also remove the services registered for it (default true)"),
            },
            ("endpoint_id", "stack", "confirm_name"),
            DANGEROUS,
            lambda a: f"DELETE stack {a.get('stack')} on environment {a.get('endpoint_id')}"
            + (" and take it down" if a.get("take_down") else ""),
        ),
    )
}


def dump(value: Any, limit: int = 30000) -> str:
    """JSON text for the assistant, cut at ``limit`` characters (with a note)."""
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    text = json.dumps(value, indent=1, default=str, ensure_ascii=False)
    if len(text) > limit:
        return text[:limit] + f"\n... cut at {limit} characters; ask for less (a filter or limit)."
    return text
