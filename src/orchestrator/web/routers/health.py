"""Liveness + readiness endpoints for Docker / Traefik / k8s-style checks.

``/healthz`` — process is up. Cheap; must never touch external services so
    the container's HEALTHCHECK doesn't cascade a false negative.
``/readyz``  — dependencies we own are ready (DB reachable). External adapters
    (Proxmox, PBS, MQTT) are NOT probed here — they can be down without the
    orchestrator being "not ready".
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from orchestrator import __version__
from orchestrator.web.deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# Plain alias. `/healthz` is the canonical name (Kubernetes/Google convention —
# the trailing "z" exists so the probe can't collide with a real app route), but
# `/health` is what most people reach for first, so serve both.
@router.get("/health", include_in_schema=False)
async def health_alias() -> dict[str, str]:
    return await healthz()


@router.get("/readyz")
async def readyz(session: SessionDep) -> dict[str, str]:
    try:
        session.exec(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"database not reachable: {exc}",
        ) from exc
    return {"status": "ready", "version": __version__}
