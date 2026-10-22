"""What is running (version, build, schema) and whether a newer build exists."""

from __future__ import annotations

import platform
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from orchestrator import __version__
from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger
from orchestrator.system.backups import BackupError, sqlite_path

log = get_logger(__name__)

STARTED_AT = time.time()
_UPDATE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_S = 300.0


def _alembic_config(settings: Settings) -> Any:
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def known_revisions(settings: Settings) -> set[str]:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config(settings))
    return {rev.revision for rev in script.walk_revisions()}


def schema_status(settings: Settings) -> dict[str, Any]:
    """The database's migration revision vs the newest this code knows."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    head = ScriptDirectory.from_config(_alembic_config(settings)).get_current_head()
    current: str | None = None
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    except Exception as exc:  # unreadable database: say so, don't 500
        return {"current": None, "head": head, "pending": None, "error": str(exc)}
    finally:
        engine.dispose()
    return {"current": current, "head": head, "pending": current != head, "error": None}


def image_tag(settings: Settings) -> str:
    ref = settings.orchestrator_image.strip()
    if not ref:
        return ""
    # ghcr.io/owner/name:tag  (a registry port also has a colon, so look after the last slash)
    last = ref.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else "latest"


def system_info(settings: Settings) -> dict[str, Any]:
    try:
        db = sqlite_path(settings)
        db_size = db.stat().st_size if db.is_file() else None
        db_label = str(db)
    except BackupError:
        db_size, db_label = None, settings.database_url.split("@")[-1]
    registry = Path(settings.services_yaml_path)
    return {
        "version": __version__,
        "git_sha": settings.orchestrator_git_sha,
        "image": settings.orchestrator_image,
        "image_tag": image_tag(settings),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "started_at": STARTED_AT,
        "uptime_s": int(time.time() - STARTED_AT),
        "dry_run": settings.dry_run,
        "database": db_label,
        "database_bytes": db_size,
        "registry": str(registry),
        "registry_exists": registry.is_file(),
        "run_migrations_on_start": settings.run_migrations_on_start,
        "schema": schema_status(settings),
        "self_backup": {
            "enabled": settings.self_backup_enabled,
            "interval_hours": settings.self_backup_interval_hours,
            "keep": settings.self_backup_keep,
        },
        "update_configured": bool(settings.self_update_command.strip()),
    }


@dataclass(frozen=True)
class UpdateCheck:
    available: bool | None  # None = could not tell
    current: str
    latest: str
    channel: str
    message: str
    url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _channel(settings: Settings) -> str:
    return settings.update_branch.strip() or (
        image_tag(settings) if image_tag(settings) in {"dev", "main"} else "main"
    )


async def check_update(settings: Settings, *, force: bool = False) -> UpdateCheck:
    """Is there a newer build of the channel this container follows?

    A ``:dev`` / ``:main`` image follows that branch's newest commit; a pinned
    ``vX.Y.Z`` image follows the latest release. Anonymous GitHub API, cached
    for five minutes. Never raises."""
    channel = _channel(settings)
    tag = image_tag(settings)
    pinned = bool(re.fullmatch(r"v?\d+\.\d+\.\d+", tag))
    key = f"{settings.update_repo}:{'release' if pinned else channel}"
    cached = _UPDATE_CACHE.get(key)
    if cached and not force and time.time() - cached[0] < _CACHE_TTL_S:
        return _build_check(settings, cached[1], pinned, channel, tag)
    base = f"https://api.github.com/repos/{settings.update_repo}"
    url = f"{base}/releases/latest" if pinned else f"{base}/commits/{channel}"
    try:
        async with httpx.AsyncClient(
            timeout=8.0, headers={"Accept": "application/vnd.github+json"}
        ) as client:
            response = await client.get(url)
        if response.status_code == 404 and pinned:
            return UpdateCheck(None, tag, "", "release", "No release has been published yet.")
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return UpdateCheck(
            None,
            settings.orchestrator_git_sha[:7] or tag,
            "",
            channel,
            f"Could not ask GitHub: {exc}",
        )
    _UPDATE_CACHE[key] = (time.time(), data)
    return _build_check(settings, data, pinned, channel, tag)


def _build_check(
    settings: Settings, data: dict[str, Any], pinned: bool, channel: str, tag: str
) -> UpdateCheck:
    if pinned:
        latest = str(data.get("tag_name", ""))
        newer = bool(latest) and latest.lstrip("v") != tag.lstrip("v")
        return UpdateCheck(
            newer,
            tag,
            latest,
            "release",
            f"A newer release, {latest}, is out." if newer else "You are on the latest release.",
            str(data.get("html_url", "")),
        )
    sha = str(data.get("sha", ""))
    mine = settings.orchestrator_git_sha.strip()
    page = str(data.get("html_url", ""))
    if not mine:
        return UpdateCheck(
            None,
            "",
            sha[:7],
            channel,
            "This image does not say which commit it was built from, so it cannot be compared. "
            f"The newest {channel} commit is {sha[:7]}.",
            page,
        )
    newer = bool(sha) and not (sha.startswith(mine) or mine.startswith(sha))
    summary = ((data.get("commit") or {}).get("message") or "").splitlines()[:1]
    return UpdateCheck(
        newer,
        mine[:7],
        sha[:7],
        channel,
        (
            (
                f"A newer {channel} build exists ({sha[:7]}"
                + (f": {summary[0]}" if summary else "")
                + ")."
            )
            if newer
            else f"You are on the newest {channel} build ({mine[:7]})."
        ),
        page,
    )
