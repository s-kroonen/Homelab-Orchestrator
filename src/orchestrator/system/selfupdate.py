"""Talking to the updater: ask for an update or a rollback, read how it went.

The updater (``scripts/updater.sh``, run as the ``updater`` compose service) owns Docker.
This process never touches it: it writes a small ``request`` file into the shared state
volume and reads the ``status.json`` the updater keeps current. The request carries no
commands and no image names - only who asked, which backup to go back to if it fails, and
optionally another tag of the image this container already runs.

Files, under ``STATE_DIR/update``: ``request``, ``status.json``, ``heartbeat``, ``log``.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.config import Settings

#: Fresher than this and the updater counts as running.
HEARTBEAT_FRESH_S = 30
#: A run that has not touched its status for this long is considered dead.
RUN_STALE_S = 15 * 60

_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
_BACKUP_RE = re.compile(r"^orchestrator-\d{8}-\d{6}(?:-[a-z0-9-]{1,40})?\.tar\.gz$")

FINISHED = ("succeeded", "rolled_back", "failed")


class UpdaterError(Exception):
    pass


def update_dir(settings: Settings) -> Path:
    return Path(settings.state_dir) / "update"


def ensure_dir(settings: Settings) -> Path:
    d = update_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - when).total_seconds())


def read_status(settings: Settings) -> dict[str, Any] | None:
    try:
        data = json.loads((update_dir(settings) / "status.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["age_s"] = _age_s(data.get("updated_at"))
    return data


def heartbeat_age_s(settings: Settings) -> float | None:
    try:
        stamp = int((update_dir(settings) / "heartbeat").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return max(0.0, time.time() - stamp)


def is_running(status: dict[str, Any] | None) -> bool:
    """A run is in flight (and has not gone silent)."""
    if not status or status.get("state") != "running":
        return False
    age = status.get("age_s")
    return age is None or age < RUN_STALE_S


def updater_online(settings: Settings, status: dict[str, Any] | None = None) -> bool:
    beat = heartbeat_age_s(settings)
    if beat is not None and beat < HEARTBEAT_FRESH_S:
        return True
    # while a run is in progress the loop is busy and does not beat
    return is_running(status if status is not None else read_status(settings))


def request_pending(settings: Settings) -> bool:
    return (update_dir(settings) / "request").exists()


def read_log(settings: Settings, lines: int = 80) -> str:
    try:
        text = (update_dir(settings) / "log").read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def overview(settings: Settings) -> dict[str, Any]:
    status = read_status(settings)
    return {
        "online": updater_online(settings, status),
        "heartbeat_age_s": heartbeat_age_s(settings),
        "running": is_running(status),
        "pending": request_pending(settings),
        "status": status,
    }


def _write_request(settings: Settings, fields: dict[str, str]) -> str:
    if not updater_online(settings):
        raise UpdaterError(
            "the updater is not running (run scripts/install.sh in the project directory)"
        )
    status = read_status(settings)
    if is_running(status) or request_pending(settings):
        raise UpdaterError("an update is already in progress")
    request_id = uuid.uuid4().hex[:12]
    body = "".join(f"{k}={v}\n" for k, v in {"ID": request_id, **fields}.items() if v)
    directory = ensure_dir(settings)
    tmp = directory / "request.tmp"
    tmp.write_text(body)
    os.replace(tmp, directory / "request")
    return request_id


def request_update(
    settings: Settings, *, by: str, backup: str | None, tag: str | None = None
) -> str:
    """Hand an update to the updater. ``backup`` is what it restores if the new version
    does not come up. Returns the request id."""
    if tag and not _TAG_RE.match(tag):
        raise UpdaterError("that is not a valid image tag")
    if backup and not _BACKUP_RE.match(backup):
        raise UpdaterError("that is not a backup file name")
    return _write_request(
        settings,
        {
            "ACTION": "update",
            "BY": by if _USER_RE.match(by) else "dashboard",
            "BACKUP": backup or "",
            "TAG": tag or "",
        },
    )


def request_rollback(settings: Settings, *, by: str, backup: str | None) -> str:
    if backup and not _BACKUP_RE.match(backup):
        raise UpdaterError("that is not a backup file name")
    return _write_request(
        settings,
        {
            "ACTION": "rollback",
            "BY": by if _USER_RE.match(by) else "dashboard",
            "BACKUP": backup or "",
        },
    )


def last_update_backup(settings: Settings) -> str | None:
    """The backup taken before the last update that finished - what a rollback restores."""
    status = read_status(settings)
    if status and status.get("action") == "update" and status.get("backup"):
        return str(status["backup"])
    return None


async def announce_last_result() -> None:
    """After a start: if the updater finished a run we have not told anyone about, say
    how it went (an update that rolled back is exactly what you want to hear about).
    Once per run id; never raises."""
    from orchestrator.config import get_settings
    from orchestrator.db.models import Setting
    from orchestrator.db.session import session_scope
    from orchestrator.logging_setup import get_logger
    from orchestrator.notify.hooks import announce

    log = get_logger(__name__)
    try:
        settings = get_settings()
        status = read_status(settings)
        if not status or status.get("state") not in FINISHED or not status.get("id"):
            return
        with session_scope() as session:
            row = session.get(Setting, "update.announced")
            if row is not None and (row.value or {}).get("id") == status["id"]:
                return
            session.add(
                Setting(key="update.announced", value={"id": status["id"]})
                if row is None
                else _touch(row, {"id": status["id"]})
            )
            session.commit()
            state = str(status["state"])
            if state == "succeeded" and status.get("from_image") == status.get("to_image"):
                return  # "already up to date" - nothing happened
            event, severity, headline = {
                "succeeded": ("update_succeeded", "info", "Orchestrator updated"),
                "rolled_back": (
                    "update_rolled_back",
                    "error",
                    "Orchestrator update FAILED - the old version was put back",
                ),
                "failed": ("update_failed", "error", "Orchestrator update FAILED"),
            }[state]
            body = str(status.get("message") or "")
            if status.get("reason"):
                body += f"\n\nWhy: {status['reason']}"
            await announce(
                session,
                level="service",
                key="orchestrator",
                name="Orchestrator",
                event=event,
                severity=severity,
                title=headline,
                body=body,
                scope={"slug": "orchestrator"},
            )
    except Exception as exc:  # a missing alert must never stop the start
        log.warning("system.update.announce_failed", error=str(exc))


def _touch(row: Any, value: dict[str, Any]) -> Any:
    row.value = value
    row.updated_at = datetime.now(UTC)
    return row
