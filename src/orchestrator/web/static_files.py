"""Static files: versioned URLs are cached for good, the rest always revalidate.

``StaticFiles`` sends ``ETag`` / ``Last-Modified`` but no ``Cache-Control``, so a
browser may keep serving a stale ``app.css`` / ``app.js`` for hours (heuristic
freshness) after an upgrade - a fixed stylesheet then looks "still broken".

Templates link files through ``asset()`` (``web/templating.py``), which adds
``?v=<content hash>``. Such a URL never changes meaning, so it is served
``immutable`` for a year: repeat page views load nothing again. A URL without
``v`` (old links, the service worker, icons) gets ``no-cache``: still
cacheable, revalidated on every load, an unchanged file costs only a ``304``.
"""

from __future__ import annotations

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

IMMUTABLE = "public, max-age=31536000, immutable"


class RevalidatingStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        versioned = b"v=" in (scope.get("query_string") or b"")
        if versioned and response.status_code == 200:
            response.headers["Cache-Control"] = IMMUTABLE
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response
