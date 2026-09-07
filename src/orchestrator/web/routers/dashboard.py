"""Dashboard + registry API — seam for phase 7.

Phase 1 exposes ONLY the read-only surface needed to prove the YAML/DB
round-trip works: list services, show the unsaved-changes badge state, and
trigger Save / Reset. Everything requires auth in phase 7 — for now the
routes are unauthenticated placeholders, and the DRY_RUN default keeps this
harmless on a fresh checkout.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from orchestrator.config import get_settings
from orchestrator.db.models import Node, Service
from orchestrator.registry.loader import (
    compute_registry_diff,
    reload_registry_from_yaml,
    save_registry_to_yaml,
)
from orchestrator.web.deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/registry/status")
async def registry_status(session: SessionDep) -> dict[str, object]:
    """Whether the DB has unsaved changes vs the on-disk YAML."""
    diff = compute_registry_diff(session)
    return {
        "dirty": diff.dirty,
        "yaml_path": diff.yaml_path,
        "yaml_hash": diff.yaml_hash,
        "db_hash": diff.db_hash,
    }


@router.get("/registry/services")
async def list_services(session: SessionDep) -> list[dict[str, object]]:
    services = session.exec(select(Service).order_by(Service.slug)).all()
    nodes = {n.id: n.name for n in session.exec(select(Node)).all()}
    return [
        {
            "slug": s.slug,
            "name": s.name,
            "enabled": s.enabled,
            "node": nodes.get(s.node_id),
            "guest_kind": s.guest_kind.value,
            "guest_id": s.guest_id,
        }
        for s in services
    ]


@router.post("/registry/save")
async def save_registry(session: SessionDep, settings: SettingsDep) -> dict[str, object]:
    """Save button — write live DB registry back to YAML."""
    _ = get_settings()  # ensure singleton warmed
    try:
        sha = save_registry_to_yaml(session, settings.services_yaml_path, actor="dashboard")
        session.commit()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not write YAML: {exc}") from exc
    return {"status": "saved", "sha256": sha, "path": str(settings.services_yaml_path)}


@router.post("/registry/reset")
async def reset_registry(session: SessionDep, settings: SettingsDep) -> dict[str, object]:
    """Reset button — re-run boot reconcile from YAML, discarding live DB edits."""
    if not settings.services_yaml_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"YAML not found at {settings.services_yaml_path}",
        )
    spec = reload_registry_from_yaml(session, settings.services_yaml_path)
    session.commit()
    return {
        "status": "reloaded",
        "path": str(settings.services_yaml_path),
        "services": len(spec.services),
        "nodes": len(spec.nodes),
        "policies": len(spec.backup_policies),
    }
