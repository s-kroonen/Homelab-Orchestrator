"""The MCP protocol layer: JSON-RPC 2.0 over Streamable HTTP, stateless.

Only what a tools-only server needs: ``initialize``, ``ping``, ``tools/list``, ``tools/call``.
No sessions and no server-to-client stream, so it works behind any proxy and survives restarts.
"""

from __future__ import annotations

import time
from typing import Any

from sqlmodel import Session

from orchestrator import __version__
from orchestrator.config import Settings
from orchestrator.db.models import McpGrant
from orchestrator.logging_setup import get_logger
from orchestrator.mcp import approvals, store
from orchestrator.mcp.scopes import SCOPES
from orchestrator.mcp.tools import (
    ACT,
    DANGEROUS,
    TOOLS,
    Tool,
    ToolContext,
    ToolError,
    dump,
    validate,
)

log = get_logger(__name__)

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "This is a homelab control plane. Start with orchestrator_status, then look before you "
    "act: list_services, get_service, recent_activity (problems=true) and linked_state show "
    "what is wrong. Starting a backup, a deploy or a wake returns a run id; poll get_run. "
    "Some actions are dangerous (restoring, powering off a node or guest, deleting a stack, "
    "deploying without guards): they do NOT run when you ask. You get a request_id and a link; "
    "a person must open the link and approve. Tell the user about the link, then check "
    "get_request(request_id). Never retry a request that is waiting."
)

# JSON-RPC error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def visible(grant: McpGrant, tool: Tool) -> bool:
    """Whether the connection may see (and call) the tool at all."""
    if tool.scope not in grant.scopes:
        return False
    return not (tool.risk == DANGEROUS and "propose" not in grant.scopes)


def _reply(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _fail(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


async def handle(
    message: Any,
    *,
    session: Session,
    adapters: Any,
    settings: Settings,
    grant: McpGrant,
) -> dict[str, Any] | None:
    """One JSON-RPC message in, the response out (None for a notification)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _fail(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if "id" not in message:  # a notification (notifications/initialized, .../cancelled)
        return None
    if not isinstance(method, str) or not isinstance(params, dict):
        return _fail(msg_id, INVALID_REQUEST, "bad request")

    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return _reply(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "homelab-orchestrator", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _reply(msg_id, {})
    if method == "tools/list":
        return _reply(msg_id, {"tools": [t.listing() for t in TOOLS.values() if visible(grant, t)]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in TOOLS:
            return _fail(msg_id, INVALID_PARAMS, f"unknown tool: {name}")
        return _reply(
            msg_id,
            await call_tool(
                TOOLS[name],
                args,
                session=session,
                adapters=adapters,
                settings=settings,
                grant=grant,
            ),
        )
    empty: dict[str, dict[str, list[Any]]] = {
        "resources/list": {"resources": []},
        "resources/templates/list": {"resourceTemplates": []},
        "prompts/list": {"prompts": []},
    }
    if method in empty:
        return _reply(msg_id, empty[method])
    return _fail(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")


async def call_tool(
    tool: Tool,
    args: Any,
    *,
    session: Session,
    adapters: Any,
    settings: Settings,
    grant: McpGrant,
) -> dict[str, Any]:
    started = time.monotonic()

    def done(status: str, summary: str, risk: str, arguments: dict[str, Any]) -> None:
        store.record_call(
            session,
            grant,
            tool=tool.name,
            risk=risk,
            args=arguments,
            status=status,
            summary=summary,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    shown = args if isinstance(args, dict) else {}
    if not visible(grant, tool):
        need = tool.scope if tool.scope not in grant.scopes else "propose"
        done(
            "refused",
            f"needs the {need!r} permission",
            tool.risk if isinstance(tool.risk, str) else ACT,
            shown,
        )
        return _text(
            f"This connection is not allowed to use {tool.name}: it needs the {need!r} "
            f"permission ({SCOPES.get(need, '')}). An admin can change that on the AI assistants page.",
            error=True,
        )
    try:
        clean = validate(tool, args)
    except ToolError as exc:
        done("error", str(exc), tool.risk if isinstance(tool.risk, str) else ACT, shown)
        return _text(f"Invalid arguments for {tool.name}: {exc}", error=True)

    risk = tool.risk_of(clean)
    if risk == DANGEROUS and "propose" not in grant.scopes:
        done("refused", "needs the 'propose' permission", risk, clean)
        return _text(
            f"This call is dangerous and this connection may not ask for dangerous actions "
            f"(it lacks the 'propose' permission): {approvals.describe(tool.name, clean)}",
            error=True,
        )
    why = "dangerous" if risk == DANGEROUS else ""
    if not why and grant.confirm_writes and tool.writes:
        why = "this connection asks before every change"
    if why:
        row, _ = await approvals.create_request(
            session, settings, grant, tool.name, clean, risk=risk, why=why
        )
        return _text(
            "NOT DONE - a person has to approve this first.\n"
            f"What: {approvals.describe(tool.name, clean)}\n"
            f"Why it needs approval: {why}\n"
            f"request_id: {row.token}\n"
            f"Approve link (for the user, they must be logged in): {approvals.request_url(settings, row.token)}\n"
            f"It stays open for {settings.mcp_approval_ttl_min} minutes. Tell the user, then "
            "check with get_request. Do not retry."
        )

    ctx = ToolContext(session, adapters, settings, actor=f"mcp:{grant.label}", grant=grant)
    try:
        output = await tool.run(ctx, clean)
    except ToolError as exc:
        done("error", str(exc), risk, clean)
        return _text(str(exc), error=True)
    except Exception as exc:
        log.exception("mcp.tool.crashed", tool=tool.name)
        done("error", f"{type(exc).__name__}: {exc}", risk, clean)
        return _text(f"{tool.name} failed unexpectedly: {type(exc).__name__}: {exc}", error=True)
    text = dump(output)
    done("ok", f"{len(text)} characters", risk, clean)
    return _text(text)
