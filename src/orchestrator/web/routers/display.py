"""Display settings: the timezone shown everywhere and used by backup schedules."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.display import (
    default_timezone,
    is_valid_timezone,
    set_timezone,
    timezone_name,
    timezone_names,
)
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/display", tags=["display"], dependencies=[Depends(require_admin)])


class DisplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: An IANA zone; "" goes back to the default.
    timezone: str


def display_state(session: Session, settings: Settings) -> dict[str, Any]:
    return {
        "timezone": timezone_name(session, settings),
        "default": default_timezone(settings),
    }


@router.get("")
async def get_display(session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    return display_state(session, settings)


@router.get("/timezones")
async def list_timezones() -> list[str]:
    return timezone_names()


@router.put("")
async def put_display(
    body: DisplayIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    name = body.timezone.strip()
    if name and not is_valid_timezone(name):
        raise HTTPException(400, f"{name!r} is not a timezone this system knows")
    before = timezone_name(session, settings)
    try:
        set_timezone(session, name)
    except ValueError as exc:  # pragma: no cover - validated above
        raise HTTPException(400, str(exc)) from exc
    audit.record(
        session,
        actor=admin.username,
        action="display.timezone",
        target="settings",
        details={"from": before, "to": name or "(default)"},
    )
    session.commit()
    # Backup schedules are cron expressions in this zone: re-register the jobs.
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.sync_jobs()
    return display_state(session, settings)
