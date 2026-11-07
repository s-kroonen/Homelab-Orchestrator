"""The API reference, for admins only (OR-8).

FastAPI's own ``/docs``, ``/redoc`` and ``/openapi.json`` are switched off in ``create_app``:
served to anyone, they listed every route and its parameters to the internet. A signed-in
admin still gets the interactive reference at ``/docs`` (Swagger UI) over
``/api/openapi.json``; a visitor is sent to the login page.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse

from orchestrator.web.auth import CurrentAdmin, get_current_admin, login_redirect
from orchestrator.web.deps import SessionDep

router = APIRouter(include_in_schema=False)


@router.get("/api/openapi.json")
async def openapi_schema(request: Request, _admin: CurrentAdmin) -> dict[str, Any]:
    schema: dict[str, Any] = request.app.openapi()
    return schema


@router.get("/docs", response_model=None)
async def api_reference(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    if get_current_admin(request, session) is None:
        return login_redirect(request)
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Orchestrator API")
