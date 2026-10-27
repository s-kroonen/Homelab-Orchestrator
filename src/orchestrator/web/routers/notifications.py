"""Notifications: the feed, the rules, and a test button.

See docs/notifications.md. Everything is admin-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, col, select

from orchestrator.audit import log as audit
from orchestrator.db.models import NotificationEvent, NotificationRule
from orchestrator.notify.engine import (
    EVENTS,
    LEVELS,
    SEVERITIES,
    Event,
    NotificationEngine,
    summarize_delivery,
)
from orchestrator.web.auth import CurrentAdmin, get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

CHANNEL_NAMES = ("ntfy", "email", "webhook")

api = APIRouter(
    prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(require_admin)]
)
pages = APIRouter(tags=["notifications"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _engine(request: Request) -> NotificationEngine:
    engine = getattr(request.app.state, "notifications", None)
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "notifications are not running")
    return engine  # type: ignore[no-any-return]


@pages.get("/notifications", response_model=None)
async def notifications_page(
    request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return _templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "admin": admin,
            "levels": LEVELS,
            "events": EVENTS,
            "severities": SEVERITIES,
            "channel_names": CHANNEL_NAMES,
        },
    )


@api.get("/channels")
async def get_channels(adapters: AdaptersDep, settings: SettingsDep) -> dict[str, Any]:
    names = adapters.notifier.channels()
    return {
        "channels": names,
        "known": list(CHANNEL_NAMES),
        "dry_run": settings.dry_run,
        "adapter": adapters.notifier.__class__.__name__,
        "hint": (
            None
            if names
            else (
                "No channel is active - alerts are logged and shown below only."
                + (" DRY_RUN=true keeps every notifier silent." if settings.dry_run else "")
                + " Set NTFY_URL, MAIL_SMTP_HOST + MAIL_TO, or NOTIFY_WEBHOOK_URL and NOTIFIER_ADAPTER=auto."
            )
        ),
    }


class TestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    __test__ = False  # not a pytest class

    channels: list[str] = Field(default_factory=list)


@api.post("/test")
async def send_test(
    body: TestIn, request: Request, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Send a test notification (to the named channels, or all) and report each outcome."""
    engine = _engine(request)
    row = await engine.emit_direct(
        session,
        Event(
            level="service",
            key="test",
            name="test",
            event="test",
            severity="info",
            title="Test notification from the orchestrator",
            body=f"Sent by {admin.username}. If you can read this, this channel works.",
            tags=["test"],
        ),
        channels=body.channels or None,
    )
    return summarize_delivery(row)


@api.get("/events")
async def list_events(
    session: SessionDep,
    limit: int = 100,
    level: str | None = None,
    target: str | None = None,
    event: str | None = None,
    suppressed: bool = True,
) -> list[dict[str, Any]]:
    stmt = select(NotificationEvent).order_by(col(NotificationEvent.id).desc())
    if level:
        stmt = stmt.where(NotificationEvent.level == level)
    if target:
        stmt = stmt.where(NotificationEvent.target == target)
    if event:
        stmt = stmt.where(NotificationEvent.event == event)
    if not suppressed:
        stmt = stmt.where(NotificationEvent.suppressed == False)  # noqa: E712
    rows = session.exec(stmt.limit(max(1, min(limit, 500)))).all()
    return [summarize_delivery(r) for r in rows]


# -- rules ---------------------------------------------------------------------


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    levels: list[str] = Field(default_factory=list)
    scope: dict[str, list[str]] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    min_severity: str = "info"
    channels: list[str] = Field(default_factory=list)


def _clean_rule(body: RuleIn) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a rule needs a name")
    for field, allowed, values in (
        ("levels", LEVELS, body.levels),
        ("events", EVENTS, body.events),
        ("channels", CHANNEL_NAMES, body.channels),
    ):
        bad = sorted(set(values) - set(allowed))
        if bad:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"unknown {field}: {', '.join(bad)} (one of {', '.join(allowed)})",
            )
    if body.min_severity not in SEVERITIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"min_severity is one of {', '.join(SEVERITIES)}"
        )
    unknown = sorted(set(body.scope) - {"slugs", "groups", "nodes"})
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"scope takes slugs, groups, nodes (not {unknown[0]})"
        )
    scope = {k: [s.strip() for s in v if s.strip()] for k, v in body.scope.items()}
    return {
        "name": name,
        "enabled": body.enabled,
        "levels": list(dict.fromkeys(body.levels)),
        "scope": {k: v for k, v in scope.items() if v},
        "events": list(dict.fromkeys(body.events)),
        "min_severity": body.min_severity,
        "channels": list(dict.fromkeys(body.channels)),
    }


def _rule_dict(rule: NotificationRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "levels": list(rule.levels or []),
        "scope": dict(rule.scope or {}),
        "events": list(rule.events or []),
        "min_severity": rule.min_severity,
        "channels": list(rule.channels or []),
    }


@api.get("/rules")
async def list_rules(session: SessionDep) -> list[dict[str, Any]]:
    return [
        _rule_dict(r) for r in session.exec(select(NotificationRule).order_by(NotificationRule.id))
    ]


def _name_free(session: Session, name: str, *, except_id: int | None = None) -> None:
    clash = session.exec(select(NotificationRule).where(NotificationRule.name == name)).first()
    if clash is not None and clash.id != except_id:
        raise HTTPException(status.HTTP_409_CONFLICT, f"a rule named {name!r} already exists")


@api.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleIn, session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    data = _clean_rule(body)
    _name_free(session, data["name"])
    rule = NotificationRule(**data)
    session.add(rule)
    session.flush()
    audit.record(
        session,
        actor=admin.username,
        action="notification_rule.created",
        target=f"rule:{rule.name}",
        details=data,
    )
    session.commit()
    session.refresh(rule)
    return _rule_dict(rule)


@api.put("/rules/{rule_id}")
async def update_rule(
    rule_id: int, body: RuleIn, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    rule = session.get(NotificationRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no rule #{rule_id}")
    data = _clean_rule(body)
    _name_free(session, data["name"], except_id=rule_id)
    for key, value in data.items():
        setattr(rule, key, value)
    session.add(rule)
    audit.record(
        session,
        actor=admin.username,
        action="notification_rule.updated",
        target=f"rule:{rule.name}",
        details=data,
    )
    session.commit()
    session.refresh(rule)
    return _rule_dict(rule)


@api.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int, session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    rule = session.get(NotificationRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no rule #{rule_id}")
    name = rule.name
    session.delete(rule)
    audit.record(
        session,
        actor=admin.username,
        action="notification_rule.deleted",
        target=f"rule:{name}",
        details={},
    )
    session.commit()
    return {"deleted": name}
