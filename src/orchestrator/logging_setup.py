"""structlog configuration with per-pipeline-run correlation IDs.

Every long-running action (wake, backup, restore) opens a bound logger with a
``correlation_id`` field so the flow can be followed across modules and across
the JSON log stream.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

from orchestrator.config import Settings, get_settings

_correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    """Return a fresh short correlation id and set it on the current context."""
    cid = uuid.uuid4().hex[:12]
    _correlation_id_var.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    _correlation_id_var.set(cid)


def get_correlation_id() -> str | None:
    return _correlation_id_var.get()


def _correlation_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    cid = _correlation_id_var.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def configure_logging(settings: Settings | None = None) -> None:
    """Wire structlog + stdlib logging together. Idempotent."""
    settings = settings or get_settings()

    level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    # At LOG_LEVEL=DEBUG the HTTP stack emits a line per socket operation, which
    # buries our own events and makes DEBUG effectively unusable for diagnosing
    # orchestrator behaviour. Keep these at WARNING unless someone explicitly
    # asks for wire-level detail via HTTP_WIRE_DEBUG=true.
    if os.getenv("HTTP_WIRE_DEBUG", "").lower() not in {"1", "true", "yes"}:
        for noisy in (
            "httpx",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "hpack",
            "asyncio",
            "aiomqtt",
            "apscheduler",
        ):
            logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _correlation_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger; call after ``configure_logging``."""
    return structlog.get_logger(name) if name else structlog.get_logger()
