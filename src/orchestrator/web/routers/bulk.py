"""Bulk actions: one request, many services / proxy hosts / nodes.

Every action here is the SAME code path as its single-item endpoint - the
handlers in ``dashboard.py``, ``proxy_hosts.py``, ``infra.py``, ``wake.py`` and
``backup.py`` were split into callable functions that both use - so
validation and audit
entries are identical to doing it one row at a time.

Items run **sequentially**, in the order given, each in its own transaction:
one failing item never rolls back or blocks the others. The response is
always a per-item report::

    {"resource": "services", "action": "disable", "params": {},
     "results": [{"id": "media", "ok": true, "message": "disabled"},
                 {"id": "nope", "ok": false, "message": "no service ..."}],
     "ok_count": 1, "failed_count": 1}

Audit: each item writes exactly what its single-item endpoint writes (for
example ``proxy_host.disabled``, ``service.updated``, ``node.power_action``),
plus one ``bulk.<resource>`` summary entry per request listing what was asked
and which items failed. See ``docs/bulk_actions.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from orchestrator.audit import log as audit
from orchestrator.db.models import ProxyHost
from orchestrator.logging_setup import get_logger
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

log = get_logger(__name__)

router = APIRouter(prefix="/api/bulk", tags=["bulk"], dependencies=[Depends(require_admin)])

#: More than this in one request is almost certainly a mistake (and a very
#: long request); the UI never sends more than one page of rows.
MAX_ITEMS = 200

SERVICE_ACTIONS = (
    "enable",
    "disable",
    "wake",
    "backup",
    "probes_required",
    "probes_optional",
    "update",
    "discover_ip",
    "wake_on_hit_on",
    "wake_on_hit_off",
)
#: Fields the "update" action may set on many services at once.
SERVICE_UPDATE_FIELDS = ("group", "backup_policy_id", "wake_trigger")
PROXY_HOST_ACTIONS = ("enable", "disable", "delete", "wake_on_hit_on", "wake_on_hit_off")
NODE_ACTIONS = ("wake", "power_off", "wake_on_hit_on", "wake_on_hit_off")


class BulkIn(BaseModel):
    """``ids`` (or the friendlier aliases ``slugs`` / ``names``) name the
    items: service slugs, proxy-host ids, node names."""

    model_config = ConfigDict(extra="forbid")

    action: str
    ids: list[str | int] = Field(default_factory=list)
    slugs: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)

    def items(self) -> list[str | int]:
        out: list[str | int] = []
        for item in [*self.ids, *self.slugs, *self.names]:
            if item not in out:
                out.append(item)
        return out


def _error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return str(detail["message"])
        return str(detail)
    return str(exc) or type(exc).__name__


def _check(body: BulkIn, allowed: tuple[str, ...]) -> list[str | int]:
    if body.action not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown action {body.action!r} - one of: {', '.join(allowed)}",
        )
    items = body.items()
    if not items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing selected - send ids")
    if len(items) > MAX_ITEMS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"at most {MAX_ITEMS} items per request")
    return items


async def _run_each(
    session: Session,
    admin: CurrentAdmin,
    *,
    resource: str,
    body: BulkIn,
    items: list[str | int],
    one: Callable[[str | int], Awaitable[str]],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in items:
        try:
            message = await one(item)
            results.append({"id": item, "ok": True, "message": message})
        except Exception as exc:  # one bad item never stops the rest
            # Whatever that item half-staged must not ride along with the next.
            session.rollback()
            log.info("bulk.item_failed", resource=resource, item=item, error=_error_text(exc))
            results.append({"id": item, "ok": False, "message": _error_text(exc)})
    ok = [r["id"] for r in results if r["ok"]]
    failed = [{"id": r["id"], "message": r["message"]} for r in results if not r["ok"]]
    audit.record(
        session,
        actor=admin.username,
        action=f"bulk.{resource}",
        target=f"bulk:{resource}",
        details={
            "action": body.action,
            "params": body.params,
            "count": len(results),
            "ok": ok,
            "failed": failed,
        },
    )
    session.commit()
    return {
        "resource": resource,
        "action": body.action,
        "params": body.params,
        "results": results,
        "ok_count": len(ok),
        "failed_count": len(failed),
    }


def _bool_param(params: dict[str, Any], key: str, default: bool = False) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"params.{key} must be true or false")
    return value


@router.post("/services")
async def bulk_services(
    body: BulkIn,
    session: SessionDep,
    admin: CurrentAdmin,
    adapters: AdaptersDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Services by slug: ``enable`` / ``disable`` / ``wake`` (fires the wake
    pipeline, returns once it is recorded) / ``backup`` (runs the backup now,
    one after another - this request returns when the last has finished) /
    ``probes_required`` / ``probes_optional`` / ``wake_on_hit_on`` /
    ``wake_on_hit_off`` / ``update`` with
    ``params: {"group": "...", "backup_policy_id": 3 | null,
    "wake_trigger": "call" | "button"}`` /
    ``discover_ip`` (IP discovery now; a stopped guest is reported, not failed)."""
    from orchestrator.web.routers.backup import run_backup
    from orchestrator.web.routers.dashboard import (
        ServiceUpdate,
        apply_service_update,
        discover_service_ip,
        set_service_probes_required,
    )
    from orchestrator.web.routers.wake import start_wake

    items = _check(body, SERVICE_ACTIONS)
    update: ServiceUpdate | None = None
    if body.action == "update":
        unknown = set(body.params) - set(SERVICE_UPDATE_FIELDS)
        if unknown or not body.params:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"update takes params {', '.join(SERVICE_UPDATE_FIELDS)}"
                + (f" (not {', '.join(sorted(unknown))})" if unknown else ""),
            )
        try:
            update = ServiceUpdate(**body.params)
        except Exception as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    verify = _bool_param(body.params, "verify", True) if body.action == "backup" else True

    async def one(item: str | int) -> str:
        slug = str(item)
        if body.action in ("enable", "disable"):
            apply_service_update(
                session, admin.username, slug, ServiceUpdate(enabled=body.action == "enable")
            )
            return f"{body.action}d"
        if body.action in ("wake_on_hit_on", "wake_on_hit_off"):
            on = body.action == "wake_on_hit_on"
            apply_service_update(
                session, admin.username, slug, ServiceUpdate(wake_on_hit_enabled=on)
            )
            return f"wake on hit {'on' if on else 'off'}"
        if body.action == "update":
            assert update is not None
            apply_service_update(session, admin.username, slug, update)
            return "updated " + ", ".join(sorted(update.model_dump(exclude_unset=True)))
        if body.action in ("probes_required", "probes_optional"):
            out = set_service_probes_required(
                session, admin.username, slug, body.action == "probes_required"
            )
            return str(out["message"])
        if body.action == "discover_ip":
            out = await discover_service_ip(session, adapters, settings, slug, actor=admin.username)
            if out["status"] == "error":
                raise RuntimeError(out["message"])
            ip = out["effective_ip"]
            if out["status"] == "found":
                return f"{out['ip']} ({out['source']})" + (" - changed" if out["changed"] else "")
            return f"{out['message']}" + (f" - keeps {ip}" if ip else "")
        if body.action == "wake":
            out = start_wake(session, adapters, settings, slug, actor=admin.username)
            return f"wake run #{out['run_id']} {'started' if out['started'] else 'already running'}"
        # backup
        run = await run_backup(
            session, adapters, settings, slug, actor=admin.username, verify=verify
        )
        if run["status"] != "succeeded":
            raise RuntimeError(f"backup run #{run['id']} {run['status']}: {run['error'] or ''}")
        return f"backup run #{run['id']} succeeded"

    return await _run_each(session, admin, resource="services", body=body, items=items, one=one)


@router.post("/proxy-hosts")
async def bulk_proxy_hosts(
    body: BulkIn,
    session: SessionDep,
    admin: CurrentAdmin,
    adapters: AdaptersDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Proxy hosts by id: ``enable`` / ``disable`` (database only: a live-managed
    host's route is switched on the next push) /
    ``delete`` / ``wake_on_hit_on`` / ``wake_on_hit_off``."""
    from orchestrator.web.routers.proxy_hosts import (
        delete_proxy_host_record,
        set_proxy_host_enabled,
        set_proxy_host_wake_on_hit,
    )

    items = _check(body, PROXY_HOST_ACTIONS)

    async def one(item: str | int) -> str:
        try:
            ph_id = int(item)
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{item!r} is not a proxy host id"
            ) from None
        ph = session.get(ProxyHost, ph_id)
        if ph is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"proxy host {ph_id} not found")
        hostname = ph.hostname
        if body.action in ("enable", "disable"):
            await set_proxy_host_enabled(
                session, admin, settings, adapters, ph, body.action == "enable"
            )
            return f"{hostname} {body.action}d"
        if body.action == "delete":
            await delete_proxy_host_record(session, admin, settings, adapters, ph)
            return f"{hostname} deleted"
        on = body.action == "wake_on_hit_on"
        await set_proxy_host_wake_on_hit(session, admin, ph, on)
        return f"{hostname} wake on hit {'on' if on else 'off'}"

    return await _run_each(session, admin, resource="proxy_hosts", body=body, items=items, one=one)


@router.post("/nodes")
async def bulk_nodes(
    body: BulkIn,
    session: SessionDep,
    admin: CurrentAdmin,
    adapters: AdaptersDep,
) -> dict[str, Any]:
    """Nodes by name: ``wake`` / ``power_off`` (``params: {"force": bool}``,
    same meaning as the single-node button) / ``wake_on_hit_on`` /
    ``wake_on_hit_off``."""
    from orchestrator.web.routers.dashboard import NodeUpdate, apply_node_update
    from orchestrator.web.routers.infra import apply_node_power

    items = _check(body, NODE_ACTIONS)
    force = _bool_param(body.params, "force", False)

    async def one(item: str | int) -> str:
        name = str(item)
        if body.action in ("wake", "power_off"):
            await apply_node_power(
                session, adapters, admin.username, name, body.action, force=force
            )
            return f"{body.action} sent" + (" (force)" if force and body.action != "wake" else "")
        on = body.action == "wake_on_hit_on"
        apply_node_update(session, admin.username, name, NodeUpdate(wake_on_hit_enabled=on))
        return f"wake on hit {'on' if on else 'off'}"

    return await _run_each(session, admin, resource="nodes", body=body, items=items, one=one)
