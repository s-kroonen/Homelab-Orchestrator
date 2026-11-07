"""Refuse state-changing requests that a browser sent from another site (OR-6).

The session cookie is ``SameSite=lax``. That stops *other domains*, but every app served under
the same domain (a sibling subdomain) counts as the same *site*: a script-injection bug in any
of them could make a signed-in admin's browser ``POST`` to the dashboard with the cookie
attached. It could not read the answers, but many endpoints act on a bare ``POST``.

So every ``POST``/``PUT``/``PATCH``/``DELETE`` must come from the dashboard itself:

* with an ``Origin`` header (every current browser sends one on these): its host must be the
  host the request was sent to, or the host of ``EXTERNAL_URL``, ``WEBAUTHN_ORIGIN`` or
  ``ORCHESTRATOR_PUBLIC_URL``;
* without ``Origin`` but with ``Sec-Fetch-Site``: it must be ``same-origin`` or ``none``;
* with neither, it is not a browser (the CLI, curl, a server) and cannot be carrying a forged
  browser request, so it passes - authentication is checked as always.

Paths authenticated by their own token, not by the cookie, are left alone: the deploy webhook
(``/hooks/``) and the assistant endpoints (``/mcp``, ``/oauth/token``, ``/oauth/revoke``,
``/oauth/register``), which other servers call.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from orchestrator.config import get_settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EXEMPT = ("/hooks/", "/mcp", "/oauth/token", "/oauth/revoke", "/oauth/register")
_FETCH_OK = frozenset({"same-origin", "none"})


def _host(url: str) -> str:
    """``host[:port]`` of a URL (or of a bare ``host[:port]``), lower-case; "" when none."""
    value = (url or "").strip()
    if not value or value == "null":
        return ""
    parts = urlsplit(value if "://" in value else f"//{value}")
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    default = {"http": 80, "https": 443}.get(parts.scheme)
    return f"{host}:{parts.port}" if parts.port and parts.port != default else host


def allowed_hosts(request_host: str) -> set[str]:
    settings = get_settings()
    hosts = {_host(request_host)}
    for url in (settings.external_url, settings.webauthn_origin, settings.orchestrator_public_url):
        hosts.add(_host(url))
    hosts.discard("")
    return hosts


def verdict(method: str, path: str, headers: dict[str, str]) -> str | None:
    """Why this request is refused, or None when it may go on. ``headers``: lower-case."""
    if method not in UNSAFE or path.startswith(EXEMPT):
        return None
    origin = headers.get("origin")
    if origin is not None:
        if _host(origin) in allowed_hosts(headers.get("host", "")):
            return None
        return f"sent from another site ({origin or 'no origin'})"
    site = headers.get("sec-fetch-site")
    if site is not None and site.lower() not in _FETCH_OK:
        return f"sent from another site (Sec-Fetch-Site: {site})"
    return None


class SameOriginMiddleware:
    """ASGI middleware: answer 403 to a state-changing request from another site."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("method") in UNSAFE:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers") or []
            }
            why = verdict(scope["method"], scope.get("path", ""), headers)
            if why is not None:
                log.warning("web.cross_site_refused", path=scope.get("path"), reason=why)
                body = json.dumps({"detail": f"Refused: {why}."}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


__all__ = ["SameOriginMiddleware", "allowed_hosts", "verdict"]
