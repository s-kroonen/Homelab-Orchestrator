"""``GET /api/events``: the live-update stream (Server-Sent Events). See orchestrator.events."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from orchestrator import events
from orchestrator.web.auth import require_admin

router = APIRouter(prefix="/api", tags=["events"], dependencies=[Depends(require_admin)])


@router.get("/events")
async def event_stream(request: Request) -> StreamingResponse:
    """One long-lived response per open page. Needs the admin session cookie, which an
    ``EventSource`` on the same origin sends by itself. Behind a proxy the route must not be
    buffered or compressed (``X-Accel-Buffering: no`` is sent for nginx-style proxies)."""
    sub = events.bus.subscribe()

    async def body() -> events.AsyncIterator[str]:
        try:
            async for frame in events.stream(sub, request.is_disconnected):
                yield frame
        finally:
            events.bus.unsubscribe(sub)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
