"""The human step: a dangerous call from an assistant waits here until a person decides.

The exact request (tool and arguments) is stored on a ``PendingApproval`` and is what runs on
approval - nothing is re-derived. The assistant gets a request id and a link; the person opens
the link (signed in as an admin), reads what will happen, and approves or denies. Only then does
anything execute, as ``mcp:<connection>``, and the outcome is written back for the assistant to
read with ``get_request``.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import McpCall, McpGrant, PendingApproval
from orchestrator.domain.enums import ApprovalKind, ApprovalStatus
from orchestrator.mcp import store
from orchestrator.mcp.tools import TOOLS, ToolContext, ToolError, dump, validate
from orchestrator.notify.hooks import announce


def _now() -> datetime:
    return datetime.now(UTC)


def request_url(settings: Settings, token: str) -> str:
    return f"{store.origin(settings)}/assistants/requests/{token}"


def is_open(row: PendingApproval) -> bool:
    return row.status is ApprovalStatus.PENDING and (store.aware(row.expires_at) or _now()) > _now()


def status_of(row: PendingApproval) -> str:
    if row.status is ApprovalStatus.PENDING and not is_open(row):
        return "expired"
    return {
        ApprovalStatus.PENDING: "waiting",
        ApprovalStatus.APPROVED: "approved",
        ApprovalStatus.DENIED: "denied",
        ApprovalStatus.EXPIRED: "expired",
        ApprovalStatus.CONSUMED: "approved",
    }[row.status]


def find(session: Session, token: str) -> PendingApproval | None:
    return session.exec(
        select(PendingApproval).where(
            PendingApproval.token == token, PendingApproval.kind == ApprovalKind.MCP_ACTION
        )
    ).one_or_none()


def describe(tool_name: str, args: dict[str, Any]) -> str:
    tool = TOOLS[tool_name]
    if tool.consequence is not None:
        return tool.consequence(args)
    return f"{tool.title}: {args}"


async def create_request(
    session: Session,
    settings: Settings,
    grant: McpGrant,
    tool_name: str,
    args: dict[str, Any],
    *,
    risk: str,
    why: str,
) -> tuple[PendingApproval, McpCall]:
    """Park the call and tell the admins. ``why`` is "dangerous" or "ask before every change"."""
    tool = TOOLS[tool_name]
    token = secrets.token_urlsafe(32)
    call = store.record_call(
        session,
        grant,
        tool=tool_name,
        risk=risk,
        args=args,
        status="pending",
        summary=f"waiting for a person ({why})",
        approval_token=token,
    )
    summary = describe(tool_name, args)
    row = PendingApproval(
        kind=ApprovalKind.MCP_ACTION,
        token=token,
        payload={
            "tool": tool_name,
            "title": tool.title,
            "args": args,
            "summary": summary,
            "why": why,
            "risk": risk,
            "grant_id": grant.id,
            "connection": grant.label,
            "client": grant.client_name,
            "call_id": call.id,
        },
        requested_by=f"mcp:{grant.label}",
        expires_at=_now() + timedelta(minutes=max(1, settings.mcp_approval_ttl_min)),
    )
    session.add(row)
    audit.record(
        session,
        actor=f"mcp:{grant.label}",
        action="mcp.requested",
        target=f"mcp:{tool_name}",
        details={"summary": summary, "args": store.redact(args), "why": why},
    )
    session.commit()
    session.refresh(row)
    await announce(
        session,
        level="service",
        key=str(args.get("slug") or args.get("node") or "mcp"),
        name=grant.label,
        event="mcp_request",
        severity="warn",
        title=f"{grant.label} wants to: {summary}",
        body=f"An AI assistant ({grant.label}) asked for this. Nothing happens until you approve it.",
        scope={"slug": str(args.get("slug") or "")},
        approve_url=request_url(settings, token),
    )
    return row, call


def request_view(
    session: Session, settings: Settings, token: str, grant: McpGrant | None = None
) -> dict[str, Any]:
    """What the assistant (and the approval page) sees about a request. A connection only sees
    its own."""
    row = find(session, token)
    if row is None or (grant is not None and row.payload.get("grant_id") != grant.id):
        raise ToolError("no such request")
    p = dict(row.payload)
    out: dict[str, Any] = {
        "request_id": row.token,
        "status": status_of(row),
        "tool": p.get("tool"),
        "title": p.get("title"),
        "summary": p.get("summary"),
        "args": store.redact(p.get("args") or {}),
        "why": p.get("why"),
        "connection": p.get("connection"),
        "requested_at": (store.aware(row.requested_at) or _now()).isoformat(),
        "expires_at": (store.aware(row.expires_at) or _now()).isoformat(),
        "decided_by": row.approved_by,
        "url": request_url(settings, row.token),
        "open": is_open(row),
    }
    if "result" in p:
        out["result"] = p["result"]
    return out


def waiting(session: Session) -> list[PendingApproval]:
    rows = session.exec(
        select(PendingApproval)
        .where(PendingApproval.kind == ApprovalKind.MCP_ACTION)
        .order_by(col(PendingApproval.requested_at).desc())
        .limit(100)
    ).all()
    return list(rows)


def _finish_call(
    session: Session, row: PendingApproval, status: str, summary: str, by: str
) -> None:
    call = session.get(McpCall, row.payload.get("call_id")) if row.payload.get("call_id") else None
    if call is not None:
        call.status = status
        call.summary = summary[:500]
        call.decided_by = by
        session.add(call)


def deny(session: Session, row: PendingApproval, admin: str) -> PendingApproval:
    if row.status is not ApprovalStatus.PENDING:
        raise ToolError(f"this request is already {status_of(row)}")
    row.status = ApprovalStatus.DENIED
    row.approved_by = admin
    row.approved_at = _now()
    session.add(row)
    _finish_call(session, row, "denied", f"denied by {admin}", admin)
    audit.record(
        session,
        actor=admin,
        action="mcp.denied",
        target=f"mcp:{row.payload.get('tool')}",
        details={"summary": row.payload.get("summary")},
    )
    session.commit()
    return row


async def approve(
    session: Session, adapters: Any, settings: Settings, row: PendingApproval, admin: str
) -> PendingApproval:
    """Run the stored request, now that ``admin`` said yes."""
    if not is_open(row):
        raise ToolError(f"this request is {status_of(row)}")
    grant = session.get(McpGrant, row.payload.get("grant_id"))
    if grant is None or grant.revoked_at is not None:
        raise ToolError("the connection that asked has been revoked - nothing was run")
    name = str(row.payload["tool"])
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}")
    args = validate(tool, row.payload.get("args"))

    row.status = ApprovalStatus.APPROVED
    row.approved_by = admin
    row.approved_at = _now()
    session.add(row)
    audit.record(
        session,
        actor=admin,
        action="mcp.approved",
        target=f"mcp:{name}",
        details={"summary": row.payload.get("summary"), "connection": grant.label},
    )
    session.commit()

    ctx = ToolContext(session, adapters, settings, actor=f"mcp:{grant.label}", grant=grant)
    result: dict[str, Any]
    try:
        output = await tool.run(ctx, args)
        text = dump(output, 4000)
        result = {"ok": True, "output": text}
        call_status, summary = "ok", f"approved by {admin}, done"
    except ToolError as exc:
        result = {"ok": False, "error": str(exc)}
        call_status, summary = "error", f"approved by {admin}, but it failed: {exc}"
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        call_status, summary = "error", f"approved by {admin}, but it failed: {exc}"
    row.payload = {**row.payload, "result": result}
    session.add(row)
    _finish_call(session, row, call_status, summary, admin)
    session.commit()
    return row
