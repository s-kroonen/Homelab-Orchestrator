"""``POST /mcp``: the MCP endpoint assistants talk to (Streamable HTTP, JSON responses).

Every request needs ``Authorization: Bearer <token>``; without one the answer is a 401 that
points at the OAuth metadata, which is how Claude's connectors discover where to sign in.
Tokens in a query string are not accepted.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from orchestrator.mcp import server, store
from orchestrator.web.deps import SessionDep, SettingsDep

router = APIRouter(tags=["mcp"])

MAX_BODY = 1_000_000


def challenge(settings: Any, error: str | None = None) -> Response:
    meta = f"{store.origin(settings)}/.well-known/oauth-protected-resource/mcp"
    parts = [f'resource_metadata="{meta}"']
    if error:
        parts.insert(0, f'error="{error}"')
    return JSONResponse(
        {"error": error or "unauthorized"},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer " + ", ".join(parts), "Cache-Control": "no-store"},
    )


@router.get("/mcp")
@router.delete("/mcp")
async def not_a_stream(settings: SettingsDep) -> Response:
    if not settings.mcp_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP is switched off")
    # No server-initiated stream and no sessions to end: this server only answers POSTs.
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})


@router.post("/mcp", response_model=None)
async def mcp(request: Request, session: SessionDep, settings: SettingsDep) -> Response:
    if not settings.mcp_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP is switched off")
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    grant = store.authenticate(session, token)
    if grant is None:
        return challenge(settings, "invalid_token" if token else None)

    raw = await request.body()
    if len(raw) > MAX_BODY:
        return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": server.PARSE_ERROR, "message": "parse error"},
            },
            status_code=400,
        )

    adapters = request.app.state.adapters
    batch = isinstance(payload, list)
    replies: list[dict[str, Any]] = []
    for message in payload if batch else [payload]:
        reply = await server.handle(
            message, session=session, adapters=adapters, settings=settings, grant=grant
        )
        if reply is not None:
            replies.append(reply)
    if not replies:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return JSONResponse(replies if batch else replies[0], headers={"Cache-Control": "no-store"})
