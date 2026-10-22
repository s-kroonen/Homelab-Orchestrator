"""Start-button tokens for the maintenance page.

A service whose ``wake_trigger`` is ``button`` is not started by merely
requesting its URL. The page it serves carries a short-lived token; pressing
the button re-requests the same URL from the browser with that token in the
``X-Orchestrator-Wake`` header, which Traefik hands on to the maintenance page.
Scrapers, monitors and apps that poll the URL never run that script, so they
see the page and start nothing.

The token is an HMAC of the service slug and the current hour, keyed with the
session secret: it needs no storage, cannot be used for another service, and
expires within two hours.
"""

from __future__ import annotations

import hashlib
import hmac
import time

#: The request header the button sends.
WAKE_HEADER = "X-Orchestrator-Wake"
_BUCKET_S = 3600


def _sign(secret: str, slug: str, bucket: int) -> str:
    msg = f"wake:{slug}:{bucket}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def make_token(secret: str, slug: str, *, now: float | None = None) -> str:
    return _sign(secret, slug, int((time.time() if now is None else now) // _BUCKET_S))


def verify_token(secret: str, slug: str, token: str | None, *, now: float | None = None) -> bool:
    if not token:
        return False
    bucket = int((time.time() if now is None else now) // _BUCKET_S)
    return any(hmac.compare_digest(token, _sign(secret, slug, b)) for b in (bucket, bucket - 1))
