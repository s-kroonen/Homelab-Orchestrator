"""Maintenance / status page — seam for phase 5.

Fail-open rule: this responder MUST render a page even when the DB /
scheduler are unhealthy — Traefik's ``errors`` middleware will send users here
when their backend is down, and 500s from us would break the last usable
signal the operator has.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["maintenance"])


@router.get("/maintenance/{service_slug}")
async def maintenance_page(service_slug: str) -> dict[str, str]:
    # Placeholder until phase 5 — returns JSON, not HTML, so an accidental
    # early Traefik wiring is obviously incomplete.
    return {
        "service": service_slug,
        "status": "unwired",
        "note": "Maintenance page lands in phase 5.",
    }
