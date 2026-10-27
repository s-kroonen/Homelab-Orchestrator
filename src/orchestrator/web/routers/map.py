"""The map's data: what runs on which box, where traffic enters, where backups go.

The page itself (``/map``) and its view (``/api/views/map``) are in ``views.py``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from orchestrator.registry.topology import build_map
from orchestrator.web.auth import require_admin
from orchestrator.web.deps import SessionDep, SettingsDep

api = APIRouter(prefix="/api", tags=["map"], dependencies=[Depends(require_admin)])
router = APIRouter()
router.include_router(api)


@api.get("/map")
async def get_map(session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    return build_map(session, settings)
