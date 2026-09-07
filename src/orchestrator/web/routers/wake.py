"""Protocol-agnostic wake endpoint — seam for phase 3.

The eventual contract (per spec §4):
    POST /wake/{service_slug}  ->  returns immediate 202 + self-refreshing HTML
    the pipeline continues in the background; state comes back via GET.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["wake"])


@router.post("/wake/{service_slug}")
async def wake(service_slug: str) -> dict[str, str]:
    return {
        "service": service_slug,
        "status": "unwired",
        "note": "Wake pipeline lands in phase 3.",
    }
