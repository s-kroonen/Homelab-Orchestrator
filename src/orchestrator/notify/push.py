"""Web Push: alerts that arrive on a phone or desktop as a native notification.

The dashboard is a PWA. A device that opens it and turns notifications on registers a
:class:`PushSubscription`; every alert the engine decides on is then encrypted to each
device and posted to its browser vendor's push service (FCM, Mozilla autopush, Apple),
which wakes the device's service worker (``static/sw.js``) to show it. It works with the
dashboard closed. The orchestrator identifies itself with a VAPID key pair created on
first use and kept in the ``Setting`` table.

Needs HTTPS (or localhost) in the browser and outbound HTTPS from this container to the
push services.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush
from sqlmodel import Session, select

from orchestrator.adapters.notifier.base import Notifier, NotifySeverity, NotSent
from orchestrator.config import Settings
from orchestrator.db.models import SETTING_PUSH_VAPID, PushSubscription, Setting
from orchestrator.db.session import session_scope
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Seconds a push service may hold an alert for a device that is off (then drops it).
PUSH_TTL_S = 12 * 3600


# -- keys ----------------------------------------------------------------------


def _public_b64(vapid: Vapid02) -> str:
    raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return str(b64urlencode(raw))


def get_vapid(session: Session) -> tuple[Vapid02, str]:
    """This orchestrator's VAPID key pair and its public key as the browser wants it
    (base64url, uncompressed point). Created and stored on first call."""
    row = session.get(Setting, SETTING_PUSH_VAPID)
    pem = (row.value or {}).get("private_pem") if row else None
    if pem:
        vapid = Vapid02.from_pem(str(pem).encode())
        return vapid, _public_b64(vapid)
    vapid = Vapid02()
    vapid.generate_keys()
    public = _public_b64(vapid)
    row = row or Setting(key=SETTING_PUSH_VAPID)
    row.value = {"private_pem": vapid.private_pem().decode(), "public_key": public}
    row.updated_at = datetime.now(UTC)
    session.add(row)
    session.commit()
    return vapid, public


def vapid_subject(settings: Settings) -> str:
    """The contact the push services see (required: a mailto: or https URL)."""
    explicit = settings.push_vapid_subject.strip()
    if explicit:
        return explicit
    origin = settings.webauthn_origin.strip()
    if origin.startswith("https://"):
        return origin
    return "mailto:admin@homelab-orchestrator.invalid"


# -- sending --------------------------------------------------------------------


def _payload(
    severity: NotifySeverity, title: str, body: str, tags: list[str] | None, url: str
) -> str:
    return json.dumps(
        {
            "title": title,
            "body": body[:600],
            "severity": severity.value,
            "tag": (title or "orchestrator")[:60],
            "url": url,
        }
    )


def send_one(
    vapid: Vapid02, subject: str, sub: PushSubscription, payload: str, *, ttl: int = PUSH_TTL_S
) -> None:
    """Blocking: encrypt ``payload`` for one device and post it. Raises
    :class:`WebPushException` on failure (``.response.status_code`` 404/410 = the device
    is gone)."""
    webpush(
        subscription_info={
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        },
        data=payload,
        vapid_private_key=vapid,
        vapid_claims={"sub": subject},
        ttl=ttl,
        timeout=15,
    )


def _gone(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in (404, 410)


def _describe(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return (f"push service said {code}: " if code else "") + str(exc)[:200]


async def deliver(
    session_factory: Callable[[], Any],
    settings: Settings,
    payload: str,
    *,
    endpoint: str | None = None,
) -> dict[str, str]:
    """Send ``payload`` to every device (or just ``endpoint``). Returns
    ``{device label: "ok" | "error: ..."}``. Devices the push service says are gone
    are forgotten; every outcome is remembered on the subscription."""
    out: dict[str, str] = {}
    with session_factory() as session:
        vapid, _public = get_vapid(session)
        subs = list(session.exec(select(PushSubscription)).all())
        if endpoint is not None:
            subs = [s for s in subs if s.endpoint == endpoint]
        subject = vapid_subject(settings)
        for sub in subs:
            name = sub.label or sub.user_agent[:40] or f"device {sub.id}"
            try:
                await asyncio.to_thread(send_one, vapid, subject, sub, payload)
            except WebPushException as exc:
                if _gone(exc):
                    session.delete(sub)
                    out[name] = "error: the device unsubscribed (removed)"
                else:
                    sub.last_error = _describe(exc)
                    session.add(sub)
                    out[name] = f"error: {sub.last_error}"
                log.warning("push.send_failed", device=name, error=str(exc)[:200])
            except Exception as exc:  # network down, bad key...
                sub.last_error = str(exc)[:200]
                session.add(sub)
                out[name] = f"error: {sub.last_error}"
                log.warning("push.send_error", device=name, error=str(exc)[:200])
            else:
                sub.last_ok_at = datetime.now(UTC)
                sub.last_error = ""
                session.add(sub)
                out[name] = "ok"
        session.commit()
    return out


class WebPushNotifier(Notifier):
    """The ``push`` channel: every device that turned notifications on."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def channels(self) -> list[str]:
        return ["push"]

    async def notify(
        self,
        *,
        severity: NotifySeverity,
        title: str,
        body: str,
        tags: list[str] | None = None,
        approve_url: str | None = None,
    ) -> None:
        url = approve_url or "/activity"
        results = await deliver(
            session_scope, self._settings, _payload(severity, title, body, tags, url)
        )
        if not results:
            raise NotSent("no device has turned notifications on (Notifications page)")
        failed = {k: v for k, v in results.items() if v != "ok"}
        if len(failed) == len(results):
            raise RuntimeError("; ".join(f"{k}: {v}" for k, v in failed.items()))


__all__ = ["PushSubscription", "WebPushNotifier", "deliver", "get_vapid", "vapid_subject"]
