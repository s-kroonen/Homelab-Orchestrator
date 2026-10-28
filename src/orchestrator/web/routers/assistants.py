"""AI assistants: who is connected, what they may do, what they did, and the requests waiting
for a person (docs/mcp.md).

The page is ``/assistants``; the MCP endpoint itself (``/mcp``) and the OAuth endpoints are
elsewhere. Everything here needs a logged-in admin.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, col, select

from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import McpGrant
from orchestrator.mcp import approvals, store
from orchestrator.mcp.scopes import ROLE_BLURBS, ROLES, SCOPES, parse_scope, role_of
from orchestrator.mcp.tools import TOOLS, ToolError
from orchestrator.web.auth import CurrentAdmin, get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.templating import templates

#: How many calls the page's "Recent calls" shows before "All calls".
RECENT_CALLS = 8

api = APIRouter(
    prefix="/api/assistants", tags=["assistants"], dependencies=[Depends(require_admin)]
)
pages = APIRouter(tags=["assistants"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = templates


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    role: str | None = None
    scopes: list[str] | None = None
    confirm_writes: bool = False
    #: Personal tokens only; empty = never expires (until revoked).
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class GrantPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=80)
    role: str | None = None
    scopes: list[str] | None = None
    confirm_writes: bool | None = None


def _scopes_from(role: str | None, scopes: list[str] | None) -> list[str]:
    if scopes is not None:
        unknown = [s for s in scopes if s not in SCOPES]
        if unknown:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown scope: {', '.join(unknown)}")
        chosen = parse_scope(scopes)
    elif role is not None:
        if role not in ROLES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown role {role!r}")
        chosen = list(ROLES[role])
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "give a role or scopes")
    if not chosen:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "choose at least one permission")
    return chosen


def overview_view(settings: Settings) -> dict[str, Any]:
    base = store.origin(settings)
    return {
        "enabled": settings.mcp_enabled,
        "mcp_url": store.mcp_url(settings),
        "authorization_server": f"{base}/.well-known/oauth-authorization-server",
        "scopes": SCOPES,
        "roles": {r: {"scopes": list(s), "blurb": ROLE_BLURBS[r]} for r, s in ROLES.items()},
        "tools": [
            {
                "name": t.name,
                "title": t.title,
                "scope": t.scope,
                "risk": (
                    "dangerous"
                    if t.risk == "dangerous"
                    else (
                        "read"
                        if t.risk == "read"
                        else "act" if not callable(t.risk) else "act, dangerous in some cases"
                    )
                ),
                "description": t.description,
            }
            for t in TOOLS.values()
        ],
    }


@api.get("/overview")
async def overview(settings: SettingsDep) -> dict[str, Any]:
    return overview_view(settings)


def _grants(session: Session) -> list[dict[str, Any]]:
    rows = session.exec(select(McpGrant).order_by(col(McpGrant.created_at).desc())).all()
    return [store.grant_view(g) for g in rows]


@api.get("/grants")
async def list_grants(session: SessionDep) -> list[dict[str, Any]]:
    return _grants(session)


@api.post("/grants", status_code=status.HTTP_201_CREATED)
async def create_grant(
    body: GrantIn, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """A personal token for a client that cannot do OAuth. Shown once."""
    scopes = _scopes_from(body.role, body.scopes)
    grant, token = store.create_personal(
        session,
        settings,
        label=body.label,
        scopes=scopes,
        admin_username=admin.username,
        confirm_writes=body.confirm_writes,
        expires_days=body.expires_days,
    )
    audit.record(
        session,
        actor=admin.username,
        action="mcp.token_created",
        target=f"mcp:{grant.label}",
        details={"role": grant.role, "scopes": scopes},
    )
    session.commit()
    return {
        **store.grant_view(grant),
        "token": token,
        "note": "Shown once. Send it as 'Authorization: Bearer <token>' to "
        + store.mcp_url(settings),
    }


def _grant(session: SessionDep, grant_id: int) -> McpGrant:
    grant = session.get(McpGrant, grant_id)
    if grant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no connection {grant_id}")
    return grant


@api.patch("/grants/{grant_id}")
async def update_grant(
    grant_id: int, body: GrantPatch, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    grant = _grant(session, grant_id)
    if grant.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "that connection is revoked")
    if body.role is not None or body.scopes is not None:
        grant.scopes = _scopes_from(body.role, body.scopes)
        grant.role = role_of(grant.scopes)
    if body.label is not None:
        grant.label = body.label.strip()
    if body.confirm_writes is not None:
        grant.confirm_writes = body.confirm_writes
    session.add(grant)
    audit.record(
        session,
        actor=admin.username,
        action="mcp.updated",
        target=f"mcp:{grant.label}",
        details={
            "role": grant.role,
            "scopes": grant.scopes,
            "confirm_writes": grant.confirm_writes,
        },
    )
    session.commit()
    return store.grant_view(grant)


@api.post("/grants/{grant_id}/revoke")
async def revoke_grant(grant_id: int, session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    _grant(session, grant_id)
    grant = store.revoke_grant(session, grant_id)
    assert grant is not None
    audit.record(
        session,
        actor=admin.username,
        action="mcp.revoked",
        target=f"mcp:{grant.label}",
        details={},
    )
    session.commit()
    return store.grant_view(grant)


@api.get("/history")
async def history(
    session: SessionDep,
    limit: int = Query(100, ge=1, le=500),
    grant_id: int | None = None,
    tool: str | None = None,
    status_: str | None = Query(None, alias="status"),
    risk: str | None = None,
) -> list[dict[str, Any]]:
    return store.history(
        session, limit=limit, grant_id=grant_id, tool=tool, status=status_, risk=risk
    )


def _requests(session: Session, settings: Settings) -> list[dict[str, Any]]:
    return [
        approvals.request_view(session, settings, row.token) for row in approvals.waiting(session)
    ]


@api.get("/requests")
async def list_requests(session: SessionDep, settings: SettingsDep) -> list[dict[str, Any]]:
    return _requests(session, settings)


def _request(session: SessionDep, token: str) -> Any:
    row = approvals.find(session, token)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such request")
    return row


@api.get("/requests/{token}")
async def get_request(token: str, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    _request(session, token)
    return approvals.request_view(session, settings, token)


@api.post("/requests/{token}/approve")
async def approve_request(
    token: str,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    row = _request(session, token)
    try:
        await approvals.approve(session, adapters, settings, row, admin.username)
    except ToolError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return approvals.request_view(session, settings, token)


@api.post("/requests/{token}/deny")
async def deny_request(
    token: str, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    row = _request(session, token)
    try:
        approvals.deny(session, row, admin.username)
    except ToolError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return approvals.request_view(session, settings, token)


# -- pages -------------------------------------------------------------------------------------


@pages.get("/assistants", response_model=None)
async def assistants_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    """What waits for you, the connections, how to connect, recent calls (the Assistants
    wireframe)."""
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    initial = {
        "overview": overview_view(settings),
        "grants": _grants(session),
        "requests": _requests(session, settings),
        "calls": store.history(session, limit=RECENT_CALLS),
    }
    return _templates.TemplateResponse(
        request, "assistants.html", {"admin": admin, "initial": initial}
    )


@pages.get("/assistants/requests/{token}", response_model=None)
async def request_page(
    token: str, request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    _request(session, token)
    return _templates.TemplateResponse(
        request, "assistant_request.html", {"admin": admin, "token": token}
    )
