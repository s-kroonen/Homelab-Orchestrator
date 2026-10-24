"""The PWA: manifest, service worker, and the Web Push endpoints.

``/manifest.webmanifest`` and ``/sw.js`` are public (the browser fetches them before and
outside a login, and the service worker must be served from the root to control the
whole app). The ``/api/push`` endpoints are admin-only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from orchestrator.adapters.notifier.base import NotifySeverity
from orchestrator.audit import log as audit
from orchestrator.db.models import PushSubscription
from orchestrator.db.session import session_scope
from orchestrator.notify.push import _payload, deliver, get_vapid
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import SessionDep, SettingsDep

_STATIC = Path(__file__).parent.parent / "static"

public = APIRouter(tags=["pwa"])
api = APIRouter(prefix="/api/push", tags=["push"], dependencies=[Depends(require_admin)])
router = APIRouter()
router.include_router(public)
router.include_router(api)

_MANIFEST: dict[str, Any] = {
    "name": "Homelab Orchestrator",
    "short_name": "Orchestrator",
    "description": "Control plane for the homelab: services, wake, backups and alerts.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0b0e14",
    "theme_color": "#0b0e14",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {
            "src": "/static/icons/icon-maskable-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ],
}


@public.get("/manifest.webmanifest")
async def manifest() -> Response:
    import json

    return Response(
        json.dumps(_MANIFEST),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@public.get("/sw.js")
async def service_worker() -> Response:
    """Served from the root so it can control every page; never cached, so a new
    version is picked up on the next visit."""
    return Response(
        (_STATIC / "sw.js").read_text(encoding="utf-8"),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


# -- Web Push ---------------------------------------------------------------------


class Keys(BaseModel):
    model_config = ConfigDict(extra="ignore")

    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class SubscribeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    endpoint: str = Field(min_length=8, max_length=1000)
    keys: Keys
    label: str = Field(default="", max_length=80)


class EndpointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str


def _row(sub: PushSubscription) -> dict[str, Any]:
    return {
        "id": sub.id,
        "label": sub.label,
        "username": sub.username,
        "user_agent": sub.user_agent,
        "created_at": (sub.created_at.replace(tzinfo=UTC) if sub.created_at else None),
        "last_ok_at": (sub.last_ok_at.replace(tzinfo=UTC) if sub.last_ok_at else None),
        "last_error": sub.last_error,
        "endpoint_tail": sub.endpoint[-18:],
        "endpoint": sub.endpoint,
    }


@api.get("/key")
async def public_key(session: SessionDep) -> dict[str, str]:
    """The VAPID public key the browser subscribes with (created on first use)."""
    _vapid, public_b64 = get_vapid(session)
    return {"public_key": public_b64}


@api.get("/subscriptions")
async def list_subscriptions(session: SessionDep) -> list[dict[str, Any]]:
    rows = session.exec(select(PushSubscription).order_by(PushSubscription.id)).all()
    return [_row(r) for r in rows]


@api.post("/subscribe")
async def subscribe(
    body: SubscribeIn, request: Request, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    if not body.endpoint.startswith("https://"):
        raise HTTPException(400, "a push endpoint must be an https URL")
    sub = session.exec(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    ).one_or_none()
    created = sub is None
    sub = sub or PushSubscription(endpoint=body.endpoint, p256dh="", auth="")
    sub.p256dh, sub.auth = body.keys.p256dh, body.keys.auth
    sub.username = admin.username
    sub.label = body.label.strip() or sub.label
    sub.user_agent = (request.headers.get("user-agent") or "")[:200]
    sub.last_error = ""
    session.add(sub)
    audit.record(
        session,
        actor=admin.username,
        action="push.subscribed" if created else "push.resubscribed",
        target=f"device:{sub.label or 'unnamed'}",
        details={"user_agent": sub.user_agent},
    )
    session.commit()
    session.refresh(sub)
    return _row(sub)


@api.post("/unsubscribe")
async def unsubscribe(
    body: EndpointIn, session: SessionDep, admin: CurrentAdmin
) -> dict[str, bool]:
    sub = session.exec(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    ).one_or_none()
    if sub is not None:
        audit.record(
            session,
            actor=admin.username,
            action="push.unsubscribed",
            target=f"device:{sub.label or 'unnamed'}",
        )
        session.delete(sub)
        session.commit()
    return {"removed": sub is not None}


@api.delete("/subscriptions/{sub_id}")
async def remove_subscription(
    sub_id: int, session: SessionDep, admin: CurrentAdmin
) -> dict[str, bool]:
    sub = session.get(PushSubscription, sub_id)
    if sub is None:
        raise HTTPException(404, "no such device")
    audit.record(
        session,
        actor=admin.username,
        action="push.removed",
        target=f"device:{sub.label or sub.id}",
    )
    session.delete(sub)
    session.commit()
    return {"removed": True}


class TestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: One device, or every device when omitted.
    endpoint: str | None = None


@api.post("/test")
async def send_test(body: TestIn, settings: SettingsDep, admin: CurrentAdmin) -> dict[str, Any]:
    """Push a test notification straight to the device(s), bypassing the rules, and
    say what each push service answered."""
    payload = _payload(
        NotifySeverity.INFO,
        "Test notification",
        f"Sent by {admin.username} at {datetime.now(UTC):%H:%M:%S} UTC. If you can read this, "
        "push works on this device.",
        ["test"],
        "/notifications",
    )
    results = await deliver(session_scope, settings, payload, endpoint=body.endpoint)
    if not results:
        raise HTTPException(409, "no device has turned notifications on yet")
    return {"results": results, "ok": all(v == "ok" for v in results.values())}
