"""Static files that browsers always revalidate.

``StaticFiles`` sends ``ETag`` / ``Last-Modified`` but no ``Cache-Control``, so a
browser may keep serving a stale ``app.css`` / ``app.js`` for hours (heuristic
freshness) after an upgrade - a fixed stylesheet then looks "still broken".
``no-cache`` keeps the files cacheable but makes the browser revalidate on every
load; unchanged files cost only a ``304``.
"""

from __future__ import annotations

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class RevalidatingStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
