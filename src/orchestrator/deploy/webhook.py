"""The deploy webhook: a Git host (or a script) tells the orchestrator "deploy this now".

The orchestrator is one place that can receive it; Portainer's own webhook keeps working and
is noticed from outside (see ``watch.py``). Coming through here, the deploy is *guarded*: the
guards run before Portainer is asked to redeploy anything.

Each service has its own secret, generated here and shown once. It lives in the database's
``setting`` table (``deploy.webhook.<slug>``), never in ``services.yaml`` and never in an
API response after it was created. GitHub signs the body with the secret (HMAC), GitLab
sends it in a header, and the generic form takes a bearer token, so the raw secret has to be
kept rather than a hash of it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session

from orchestrator.config import Settings
from orchestrator.db.models import Setting


def _key(slug: str) -> str:
    return f"deploy.webhook.{slug}"


def has_secret(session: Session, slug: str) -> bool:
    row = session.get(Setting, _key(slug))
    return bool(row and (row.value or {}).get("secret"))


def rotate_secret(session: Session, slug: str) -> str:
    """Make a new secret (replacing any old one) and return it. This is the only time it
    is ever shown."""
    secret = secrets.token_urlsafe(32)
    row = session.get(Setting, _key(slug)) or Setting(key=_key(slug))
    row.value = {"secret": secret}
    row.updated_at = datetime.now(UTC)
    session.add(row)
    session.commit()
    return secret


def remove_secret(session: Session, slug: str) -> bool:
    row = session.get(Setting, _key(slug))
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def _secret(session: Session, slug: str) -> str:
    row = session.get(Setting, _key(slug))
    return str((row.value or {}).get("secret", "")) if row else ""


def webhook_url(settings: Settings, slug: str) -> str:
    base = (settings.external_url or settings.orchestrator_public_url).strip().rstrip("/")
    return f"{base}/hooks/deploy/{slug}" if base else f"/hooks/deploy/{slug}"


def setup_hint(provider: str, url: str) -> list[str]:
    """What to enter on the other side. The secret itself is never part of it."""
    if provider == "gitlab":
        return [
            "GitLab > Settings > Webhooks > Add new webhook",
            f"URL: {url}",
            "Secret token: the secret shown when the webhook was created",
            "Trigger: Push events (pick the branch)",
        ]
    if provider == "github":
        return [
            "GitHub > Settings > Webhooks > Add webhook",
            f"Payload URL: {url}",
            "Content type: application/json",
            "Secret: the secret shown when the webhook was created",
            "Events: Just the push event",
        ]
    return [
        f"POST {url}",
        "Header: Authorization: Bearer <the secret shown when the webhook was created>",
        'Optional JSON body: {"ref": "refs/heads/dev", "sha": "..."} (checked against the branch)',
    ]


# -- verifying a request -------------------------------------------------------------------------


class WebhookRejected(Exception):
    """Not authenticated (or not a webhook at all)."""


@dataclass(frozen=True)
class Delivery:
    """A verified delivery, reduced to what the pipeline records."""

    provider: str
    event: str
    ref: str = ""
    sha: str = ""
    pusher: str = ""
    #: a push or a plain trigger that should start a deploy (False = acknowledge only)
    deploys: bool = True
    note: str = ""

    def as_detail(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            # not "event": the step recorder logs these as keyword arguments
            "hook_event": self.event,
            "ref": self.ref,
            "commit": self.sha[:12],
            "pusher": self.pusher,
        }


def _equal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _bearer(headers: dict[str, str], query: dict[str, str]) -> str:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-deploy-token", "") or query.get("token", "")


def verify(
    session: Session,
    slug: str,
    provider: str,
    *,
    headers: dict[str, str],
    query: dict[str, str],
    body: bytes,
    payload: dict[str, Any],
) -> Delivery:
    """Check the request against the service's secret and parse it. ``headers`` keys are
    lower-case. Raises :class:`WebhookRejected` when it does not authenticate."""
    secret = _secret(session, slug)
    if not secret:
        raise WebhookRejected("no secret has been created for this webhook")

    if provider == "gitlab":
        if not _equal(headers.get("x-gitlab-token", ""), secret):
            raise WebhookRejected("bad token")
        event = headers.get("x-gitlab-event", "")
        if event and event != "Push Hook":
            return Delivery("gitlab", event, deploys=False, note=f"{event} ignored")
        return Delivery(
            "gitlab",
            event or "Push Hook",
            ref=str(payload.get("ref", "")),
            sha=str(payload.get("checkout_sha") or payload.get("after") or ""),
            pusher=str(payload.get("user_username") or payload.get("user_name") or ""),
        )

    if provider == "github":
        theirs = headers.get("x-hub-signature-256", "")
        ours = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not _equal(theirs, ours):
            raise WebhookRejected("bad signature")
        event = headers.get("x-github-event", "")
        if event == "ping":
            return Delivery("github", "ping", deploys=False, note="pong")
        if event and event != "push":
            return Delivery("github", event, deploys=False, note=f"{event} ignored")
        return Delivery(
            "github",
            event or "push",
            ref=str(payload.get("ref", "")),
            sha=str(payload.get("after") or ""),
            pusher=str((payload.get("pusher") or {}).get("name", "")),
        )

    if not _equal(_bearer(headers, query), secret):
        raise WebhookRejected("bad token")
    return Delivery(
        "generic",
        "trigger",
        ref=str(payload.get("ref", "")),
        sha=str(payload.get("sha") or payload.get("commit") or ""),
        pusher=str(payload.get("pusher") or payload.get("user") or ""),
    )


def branch_matches(want: str, ref: str) -> bool:
    """No filter, or the push is to that branch. A delivery that names no ref (the generic
    form) is accepted: the sender chose to trigger it."""
    want = want.strip()
    if not want or not ref:
        return True
    return ref in {want, f"refs/heads/{want}"}
