"""Connectivity, per connection.

``GET /api/infra/overview`` answers one question for every connection: is it reachable? One row
per *parent* connection (a Proxmox, a Portainer...), never the guests, stacks or containers
inside it. ``GET /api/infra/connections/{id}`` is what you get when you open one: what is up
inside that connection (Portainer's environments, Proxmox's nodes and guests, the power manager's
targets...), with a link to the page that goes deeper.

``/api/infra/status`` (the ``orchestrator-cli check`` view, one flat list) is unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from orchestrator.connections import runtime
from orchestrator.connections.checks import CheckResult, check_connection
from orchestrator.connections.model import Connection
from orchestrator.web.auth import require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.routers.infra import node_health_view, power_checks

router = APIRouter(prefix="/api/infra", tags=["infra"], dependencies=[Depends(require_admin)])

#: A section of a detail page must not stall the page.
_SECTION_TIMEOUT_S = 20.0


def _web_url(conn: Connection) -> str:
    """Where a person opens this system in a browser, when it has one."""
    v = conn.value
    spec = conn.spec
    field = {f.name: f for f in spec.fields}
    if conn.kind == "portainer":
        return str(v(field["public_url"]) or v(field["url"]) or "")
    if conn.kind in {"proxy", "traefik_api", "haos", "ntfy", "webhook"}:
        return str(v(field["url"]) or "")
    if conn.kind in {"proxmox", "pbs"}:
        host = str(v(field["host"]) or "")
        return f"https://{host}:{v(field['port'])}" if host else ""
    return ""


def _head(conn: Connection) -> dict[str, Any]:
    return {
        "id": conn.id,
        "kind": conn.kind,
        "kind_title": conn.spec.title,
        "name": conn.label,
        "site": conn.site,
        "scope": conn.scope,
        "default": conn.default,
        "source": conn.source,
        "enabled": conn.enabled,
        "configured": conn.configured,
        "web_url": _web_url(conn),
    }


def _state(conn: Connection, result: CheckResult | None) -> tuple[str, str, int | None]:
    if not conn.enabled:
        return "off", "switched off", None
    if not conn.configured or result is None:
        return "incomplete", "not filled in", None
    if result.ok is None:
        return "unchecked", result.detail, None
    return ("up" if result.ok else "down"), result.detail, result.ms


@router.get("/overview")
async def overview(settings: SettingsDep) -> dict[str, Any]:
    """One row per connection: reachable or not. Checks run side by side."""
    conns = runtime.current().all
    runnable = [c for c in conns if c.enabled and c.configured]
    results = dict(
        zip(
            (c.id for c in runnable),
            await asyncio.gather(*(check_connection(c, settings) for c in runnable)),
            strict=True,
        )
    )
    rows = []
    for conn in conns:
        state, detail, ms = _state(conn, results.get(conn.id))
        rows.append({**_head(conn), "state": state, "detail": detail, "ms": ms})
    return {
        "dry_run": settings.dry_run,
        "error": runtime.current().error,
        "connections": rows,
    }


# -- one connection ------------------------------------------------------------------------------


async def _guarded(coro: Any) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(coro, _SECTION_TIMEOUT_S)  # type: ignore[no-any-return]
    except TimeoutError:
        return {"error": f"no answer in {int(_SECTION_TIMEOUT_S)} s"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


async def _portainer(adapters: Any, conn: Connection) -> dict[str, Any]:
    adapter = adapters.portainer_for(conn.id)
    if adapter is None or not adapter.configured:
        return {"error": "this Portainer is not active (switched off or not filled in)"}
    envs = await adapter.list_environments()
    return {
        "environments": [
            {
                "id": e.id,
                "name": e.name,
                "type": e.type,
                "status": e.status,
                "running": e.running,
                "stopped": e.stopped,
                "stacks": e.stacks,
            }
            for e in envs
        ],
    }


async def _proxmox(session: Any, adapters: Any) -> dict[str, Any]:
    from sqlmodel import select

    from orchestrator.db.models import Node

    nodes = session.exec(select(Node).order_by(Node.name)).all()
    views = await asyncio.gather(*(node_health_view(adapters, n) for n in nodes))
    online: dict[str, bool] = {}
    cluster_error = ""
    try:
        cluster = await adapters.proxmox.cluster_status()
        online = {n.name: n.online for n in cluster.nodes}
    except Exception as exc:
        cluster_error = str(exc)
    guests = await adapters.proxmox.list_guests(None)
    return {
        "cluster_error": cluster_error,
        "nodes": [
            {
                "name": n.name,
                "address": n.address,
                "always_on": n.always_on,
                "online": online.get(n.name),
                "reachable": v.get("reachable"),
                "version": v.get("version"),
                "latency_ms": v.get("latency_ms"),
                "error": v.get("error"),
            }
            for n, v in zip(nodes, views, strict=True)
        ],
        "guests": {
            "total": len(guests),
            "running": sum(1 for g in guests if g.status == "running"),
        },
    }


async def _pbs(settings: Any, adapters: Any) -> dict[str, Any]:
    ds = await adapters.pbs.datastore_status(settings.pbs_datastore)
    return {
        "datastore": {
            "name": ds.name,
            "total_bytes": ds.total_bytes,
            "used_bytes": ds.used_bytes,
            "used_fraction": round(ds.used_fraction, 4),
        }
    }


async def _proxy(adapters: Any) -> dict[str, Any]:
    routes = await adapters.proxy.list_routes()
    return {"routes": None if routes is None else len(routes)}


_LINKS: dict[str, list[tuple[str, str]]] = {
    "proxmox": [("Nodes & guests", "/nodes")],
    "pbs": [("Snapshots", "/backup-policies?tab=snapshots")],
    "proxy": [("Proxy hosts", "/proxy-hosts")],
}


@router.get("/connections/{ident}")
async def connection_detail(
    ident: str, settings: SettingsDep, adapters: AdaptersDep, session: SessionDep
) -> dict[str, Any]:
    """What is up inside one connection."""
    conn = runtime.current().get(ident)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no connection called {ident!r}")
    head = _head(conn)
    if not (conn.enabled and conn.configured):
        state, detail, ms = _state(conn, None)
        return {**head, "state": state, "detail": detail, "ms": ms, "links": [], "sections": {}}

    check_task = asyncio.create_task(check_connection(conn, settings))
    sections: dict[str, Any] = {}
    if conn.kind == "portainer":
        sections = await _guarded(_portainer(adapters, conn))
        links = [("Open the Portainer page", f"/portainer?instance={ident}")]
    else:
        links = list(_LINKS.get(conn.kind, []))
        if conn.kind == "proxmox":
            sections = await _guarded(_proxmox(session, adapters))
        elif conn.kind == "pbs":
            sections = await _guarded(_pbs(settings, adapters))
        elif conn.kind == "proxy":
            sections = await _guarded(_proxy(adapters))
        elif conn.kind == "power":
            sections = await _guarded(_power(session, adapters))
    state, detail, ms = _state(conn, await check_task)
    return {
        **head,
        "state": state,
        "detail": detail,
        "ms": ms,
        "links": [{"label": label, "href": href} for label, href in links],
        "sections": sections,
    }


async def _power(session: Any, adapters: Any) -> dict[str, Any]:
    rows = await power_checks(session, adapters)
    return {
        "targets": [
            {
                "target": r["target"],
                "reachable": bool(r.get("reachable")),
                "detail": (r.get("result") or {}).get("detail") or r.get("error") or "",
            }
            for r in rows
        ]
    }
