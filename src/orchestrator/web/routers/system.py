"""The orchestrator managing itself: info, backups, restore, schema, update, restart.

Everything is admin-only. The logic lives in ``orchestrator.system`` so the CLI
(``orchestrator-cli system``) does exactly the same thing. See docs/system.md.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from orchestrator.audit import log as audit
from orchestrator.system import backups as bk
from orchestrator.system import info as sysinfo
from orchestrator.system import selfupdate as updater
from orchestrator.system import update as upd
from orchestrator.web.auth import CurrentAdmin, get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import SessionDep, SettingsDep

api = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(require_admin)])
pages = APIRouter(tags=["system"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _audit(session: Session, admin: Any, action: str, target: str, **details: Any) -> None:
    audit.record(session, actor=admin.username, action=action, target=target, details=details)
    session.commit()


def _fail(exc: Exception, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(code, str(exc))


@pages.get("/system", response_model=None)
async def system_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return _templates.TemplateResponse(request, "system.html", {"admin": admin})


@api.get("/info")
async def get_info(settings: SettingsDep) -> dict[str, Any]:
    info = await asyncio.to_thread(sysinfo.system_info, settings)
    info["pending_restore"] = bk.pending_restore(settings)
    return info


@api.get("/backups")
async def get_backups(settings: SettingsDep) -> list[dict[str, Any]]:
    return await asyncio.to_thread(lambda: [b.as_dict() for b in bk.list_backups(settings)])


class BackupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = ""


@api.post("/backups", status_code=status.HTTP_201_CREATED)
async def make_backup(
    body: BackupIn, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    try:
        info = await asyncio.to_thread(
            bk.create_backup, settings, reason="manual", note=body.note[:200]
        )
    except bk.BackupError as exc:
        raise _fail(exc) from exc
    _audit(session, admin, "system.backup_created", f"backup:{info.name}")
    return info.as_dict()


@api.get("/backups/{name}/download")
async def download_backup(name: str, settings: SettingsDep) -> FileResponse:
    try:
        path = bk.backup_path(settings, name)
    except bk.BackupError as exc:
        raise _fail(exc, status.HTTP_404_NOT_FOUND) from exc
    return FileResponse(path, media_type="application/gzip", filename=name)


@api.delete("/backups/{name}")
async def remove_backup(
    name: str, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    try:
        bk.delete_backup(settings, name)
    except bk.BackupError as exc:
        raise _fail(exc, status.HTTP_404_NOT_FOUND) from exc
    _audit(session, admin, "system.backup_deleted", f"backup:{name}")
    return {"deleted": name}


@api.post("/backups/{name}/restore")
async def stage_restore(
    name: str, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Stage a restore; it is applied on the next start (use Restart)."""
    try:
        known = await asyncio.to_thread(sysinfo.known_revisions, settings)
        marker = await asyncio.to_thread(bk.stage_restore, settings, name, known_revisions=known)
    except bk.BackupError as exc:
        raise _fail(exc) from exc
    _audit(session, admin, "system.restore_staged", f"backup:{name}")
    return {**marker, "message": "Staged. It is applied when the orchestrator restarts."}


@api.delete("/restore")
async def cancel_restore(
    session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    cancelled = bk.cancel_restore(settings)
    if cancelled:
        _audit(session, admin, "system.restore_cancelled", "system")
    return {"cancelled": cancelled}


@api.post("/migrate")
async def migrate(
    session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Upgrade the database schema to the newest this version knows, after a backup."""
    status_before = await asyncio.to_thread(sysinfo.schema_status, settings)
    if not status_before["pending"]:
        return {"changed": False, "schema": status_before, "message": "Already up to date."}
    backup_name = None
    with contextlib.suppress(bk.BackupError):
        backup_name = (
            await asyncio.to_thread(bk.create_backup, settings, reason="pre-migration")
        ).name

    def _upgrade() -> None:
        from alembic import command

        command.upgrade(sysinfo._alembic_config(settings), "head")

    try:
        await asyncio.to_thread(_upgrade)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"migration failed: {exc}" + (f" (backup: {backup_name})" if backup_name else ""),
        ) from exc
    after = await asyncio.to_thread(sysinfo.schema_status, settings)
    _audit(session, admin, "system.migrated", "database", to=after["current"], backup=backup_name)
    return {
        "changed": True,
        "schema": after,
        "backup": backup_name,
        "message": "Database upgraded.",
    }


@api.get("/update")
async def update_check(settings: SettingsDep, refresh: bool = False) -> dict[str, Any]:
    check = await sysinfo.check_update(settings, force=refresh)
    return {
        **check.as_dict(),
        "configured": bool(settings.self_update_command.strip())
        or updater.updater_online(settings),
        "method": (
            "updater"
            if updater.updater_online(settings)
            else ("command" if settings.self_update_command.strip() else "manual")
        ),
        "updater": updater.overview(settings),
        "manual_steps": upd.MANUAL_STEPS,
    }


@api.post("/update")
async def update_now(
    session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Back up, then run the configured update command on the host."""
    result = await upd.run_update(settings, by=admin.username)
    _audit(
        session,
        admin,
        "system.update_started",
        "orchestrator",
        ran=result.ran,
        ok=result.ok,
        method=result.method,
        request=result.request_id,
        backup=result.backup.name if result.backup else None,
    )
    return result.as_dict()


@api.get("/update/status")
async def update_status(settings: SettingsDep) -> dict[str, Any]:
    """Where the updater is: online?, what it is doing, how the last run ended."""
    return updater.overview(settings)


@api.get("/update/log")
async def update_log(settings: SettingsDep) -> dict[str, str]:
    return {"log": updater.read_log(settings)}


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The backup to restore with it; default: the one taken before the last update.
    backup: str | None = None


@api.post("/update/rollback")
async def rollback(
    body: RollbackIn, session: SessionDep, settings: SettingsDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Go back to the previous build (and the data backup taken before the update).
    Takes a backup of the CURRENT data first, so the rollback itself can be undone."""
    backup_name = body.backup or updater.last_update_backup(settings)
    try:
        safety = await asyncio.to_thread(bk.create_backup, settings, reason="pre-rollback")
    except bk.BackupError as exc:
        raise _fail(exc) from exc
    try:
        request_id = updater.request_rollback(settings, by=admin.username, backup=backup_name)
    except updater.UpdaterError as exc:
        raise _fail(exc, status.HTTP_409_CONFLICT) from exc
    _audit(
        session,
        admin,
        "system.rollback_requested",
        "orchestrator",
        request=request_id,
        restores=backup_name,
        safety_backup=safety.name,
    )
    return {
        "request_id": request_id,
        "restores": backup_name,
        "safety_backup": safety.name,
        "message": "Rollback handed to the updater.",
    }


@api.post("/restart")
async def restart(session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    """Stop the process; Docker's restart policy starts it again."""
    _audit(session, admin, "system.restart", "orchestrator")
    upd.schedule_restart()
    return {"restarting": True}
