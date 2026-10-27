"""The page a browser gets for an address that leads nowhere.

Alerts and bookmarks outlive what they point at. For ``/services/<slug>`` the page checks the
archive and, when the service was deleted lately, says so and offers to restore it; anything
else gets a plain "not found" with a way back. API and machine callers keep the JSON 404.
"""

from __future__ import annotations

import re

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from orchestrator.db.session import session_scope
from orchestrator.logging_setup import get_logger
from orchestrator.registry import archive as arch
from orchestrator.web.auth import get_current_admin
from orchestrator.web.templating import templates

log = get_logger(__name__)

_templates = templates
_SERVICE_PAGE = re.compile(r"^/services/([^/]+)/?$")
_HOST_PAGE = re.compile(r"^/proxy-hosts/(\d+)/edit/?$")
_POLICY_PAGE = re.compile(r"^/backup-policies/([^/]+)/edit/?$")
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
    kind, ref = "page", ""
    if m := _SERVICE_PAGE.match(path):
        kind, ref = arch.KIND_SERVICE, m.group(1)
    elif m := _HOST_PAGE.match(path):
        kind, ref = arch.KIND_PROXY_HOST, m.group(1)
    elif m := _POLICY_PAGE.match(path):
        kind, ref = arch.KIND_POLICY, m.group(1)
    context: dict[str, object] = {
        "admin": None,
        "path": path,
        "slug": ref,
        "kind": kind,
        "archived": None,
    }
    try:
        with session_scope() as session:
            admin = get_current_admin(request, session)
            # Only a signed-in admin learns what used to exist.
            if admin is not None and ref:
                row = (
                    arch.latest_ref(session, kind, ref)
                    if kind == arch.KIND_PROXY_HOST
                    else arch.latest_for(session, ref, kind)
                )
                context["archived"] = arch.describe(row) if row is not None else None
            context["admin"] = {"username": admin.username} if admin is not None else None
    except Exception as exc2:  # the not-found page must never be the thing that breaks
        log.warning("not_found.lookup_failed", error=str(exc2))
    return _templates.TemplateResponse(request, "not_found.html", context, status_code=404)
