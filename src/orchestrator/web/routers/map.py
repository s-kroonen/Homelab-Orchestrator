"""The map: what runs on which box, where traffic enters, where backups go."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from orchestrator.registry.topology import build_map
from orchestrator.web.auth import get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import SessionDep, SettingsDep

api = APIRouter(prefix="/api", tags=["map"], dependencies=[Depends(require_admin)])
pages = APIRouter(tags=["map"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@pages.get("/map", response_model=None)
async def map_page(request: Request, session: SessionDep) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return _templates.TemplateResponse(request, "map.html", {"admin": admin})


@api.get("/map")
async def get_map(session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    return build_map(session, settings)
