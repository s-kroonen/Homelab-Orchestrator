"""Display settings: the timezone times are shown in and cron schedules run in.

Stored in the ``Setting`` table (key :data:`SETTING_DISPLAY`) so it can be changed from
the dashboard without a restart; the ``TIMEZONE`` env var (or the container's own
``TZ``) is only the default until someone sets it. Everything is stored as UTC - this
only decides how it is shown and when a cron expression like ``0 3 * * *`` means 03:00.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlmodel import Session

from orchestrator.config import Settings
from orchestrator.db.models import Setting

SETTING_DISPLAY = "display"


@lru_cache(maxsize=1)
def timezone_names() -> list[str]:
    """Every IANA zone this system knows (what the picker offers)."""
    return sorted(available_timezones() | {"UTC"})


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return bool(name.strip())


def default_timezone(settings: Settings) -> str:
    """What applies until one is chosen: ``TIMEZONE``, then the container's ``TZ``."""
    for candidate in (settings.timezone, os.environ.get("TZ", "")):
        candidate = (candidate or "").strip()
        if candidate and is_valid_timezone(candidate):
            return candidate
    return "UTC"


def timezone_name(session: Session, settings: Settings) -> str:
    row = session.get(Setting, SETTING_DISPLAY)
    chosen = str((row.value or {}).get("timezone", "")).strip() if row else ""
    if chosen and is_valid_timezone(chosen):
        return chosen
    return default_timezone(settings)


def get_timezone(session: Session, settings: Settings) -> ZoneInfo:
    return ZoneInfo(timezone_name(session, settings))


def set_timezone(session: Session, name: str) -> str:
    """Remember ``name`` (an IANA zone). Raises ``ValueError`` if it is not one.
    An empty name goes back to the default."""
    name = name.strip()
    if name and not is_valid_timezone(name):
        raise ValueError(f"{name!r} is not a timezone this system knows (try Europe/Amsterdam)")
    row = session.get(Setting, SETTING_DISPLAY) or Setting(key=SETTING_DISPLAY)
    row.value = {**(row.value or {}), "timezone": name}
    row.updated_at = datetime.now(UTC)
    session.add(row)
    session.flush()
    return name
