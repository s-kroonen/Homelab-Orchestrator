"""The page a browser gets for an address that leads nowhere.

Alerts and bookmarks outlive what they point at. For ``/services/<slug>`` the page checks the
archive and, when the service was deleted lately, says so and offers to restore it; anything
else gets a plain "not found" with a way back. API and machine callers keep the JSON 404.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from orchestrator.db.session import session_scope
from orchestrator.logging_setup import get_logger
from orchestrator.registry import archive as arch
from orchestrator.web.auth import get_current_admin

log = get_logger(__name__)

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_SERVICE_PAGE = re.compile(r"^/services/([^/]+)/?$")
#: Paths whose callers are not people looking at a dashboard.
_NOT_PAGES = ("/api/", "/static/", "/mcp", "/oauth/", "/.well-known/", "/maintenance/", "/wake/")


def wants_page(request: Request) -> bool:
    path = request.url.path
    if request.method != "GET" or path.startswith(_NOT_PAGES):
        return False
    return "text/html" in request.headers.get("accept", "")


async def not_found_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code != 404 or not wants_page(request):
        return await http_exception_handler(request, exc)  # type: ignore[arg-type]
    path = request.url.path
    match = _SERVICE_PAGE.match(path)
    slug = match.group(1) if match else ""
    context: dict[str, object] = {
        "admin": None,
        "path": path,
        "slug": slug,
        "kind": "service" if slug else "page",
        "archived": None,
    }
    try:
        with session_scope() as session:
            admin = get_current_admin(request, session)
            # Only a signed-in admin learns what used to exist.
            if admin is not None and slug:
                row = arch.latest_for(session, slug)
                context["archived"] = arch.describe(row) if row is not None else None
            context["admin"] = {"username": admin.username} if admin is not None else None
    except Exception as exc2:  # the not-found page must never be the thing that breaks
        log.warning("not_found.lookup_failed", error=str(exc2))
    return _templates.TemplateResponse(request, "not_found.html", context, status_code=404)
