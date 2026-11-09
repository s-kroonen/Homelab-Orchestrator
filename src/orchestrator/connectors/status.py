"""The last known status of every source: what the sidebar dots, the All sources list and a
source page's header show at first paint.

A summary means contacting the source, so pages never wait for one: they read this cache, and
it is refreshed in the background (every ``REFRESH_S``, and by Test / Test all). A source that
was never checked reads as "unknown".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any

from orchestrator.config import Settings
from orchestrator.connectors.base import Summary
from orchestrator.connectors.registry import Source, all_sources, context
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

REFRESH_S = 60
TIMEOUT_S = 20.0

#: source id -> (summary, when it was taken: time.time())
_cache: dict[str, tuple[Summary, float]] = {}


def cached(source_id: str) -> tuple[Summary, float] | None:
    return _cache.get(source_id)


def as_dict(source_id: str) -> dict[str, Any]:
    hit = _cache.get(source_id)
    if hit is None:
        return {"state": "unknown", "label": "not checked yet", "checked_at": None}
    summary, at = hit
    return {**asdict(summary), "checked_at": at}


async def refresh(source: Source, settings: Settings, adapters: Any) -> Summary:
    """Check one source now and remember the result. Never raises."""
    if not source.enabled:
        summary = Summary(state="off", label="switched off")
    else:
        try:
            ctx = context(source, settings, adapters)
            summary = await asyncio.wait_for(source.connector.summary(ctx), TIMEOUT_S)
        except TimeoutError:
            summary = Summary(
                state="fail", label="unreachable", detail=f"no answer in {int(TIMEOUT_S)} s"
            )
        except Exception as exc:
            summary = Summary(
                state="fail", label="error", detail=f"{type(exc).__name__}: {exc}"[:300]
            )
    _cache[source.id] = (summary, time.time())
    return summary


async def refresh_all(settings: Settings, adapters: Any) -> None:
    sources = all_sources(settings)
    await asyncio.gather(*(refresh(s, settings, adapters) for s in sources))
    gone = set(_cache) - {s.id for s in sources}
    for ident in gone:
        _cache.pop(ident, None)


async def keep_fresh(adapters: Any) -> None:
    """Background task: refresh every source every ``REFRESH_S``, with the settings of the
    moment (connections change without a restart)."""
    from orchestrator.config import get_settings

    while True:
        try:
            await refresh_all(get_settings(), adapters)
        except Exception as exc:
            log.warning("sources.status_failed", error=str(exc))
        await asyncio.sleep(REFRESH_S)


def forget() -> None:
    _cache.clear()


__all__ = ["as_dict", "cached", "forget", "keep_fresh", "refresh", "refresh_all"]
