"""The archive: deleted services waiting out their retention (``ARCHIVE_RETENTION_DAYS``).

List, restore and purge-now. The logic is in ``orchestrator.registry.archive``; the CLI
(``orchestrator-cli archive``) calls the same functions. See docs/archive.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from orchestrator.registry import archive as arch
from orchestrator.web.auth import CurrentAdmin, get_current_admin, require_admin
from orchestrator.web.deps import SessionDep

api = APIRouter(prefix="/api/archive", tags=["archive"], dependencies=[Depends(require_admin)])
pages = APIRouter(tags=["archive"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@pages.get("/archive", response_model=None)
async def archive_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return RedirectResponse("/login?next=/archive", status_code=303)
    return _templates.TemplateResponse(request, "archive.html", {"admin": admin})


@api.get("")
async def list_archive(session: SessionDep) -> list[dict[str, Any]]:
    arch.purge_expired(session)  # anything past its time is gone before it is listed
    return [arch.describe(r) for r in arch.list_archive(session)]


@api.post("/{archive_id}/restore")
async def restore(archive_id: int, session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    try:
        result = arch.restore(session, archive_id, admin.username)
    except arch.ArchiveError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {
        "status": "restored",
        "slug": result.slug,
        "restored": result.restored,
        "warnings": result.warnings,
    }


@api.delete("/{archive_id}")
async def purge(archive_id: int, session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    try:
        slug = arch.purge_now(session, archive_id, admin.username)
    except arch.ArchiveError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"status": "purged", "slug": slug}
