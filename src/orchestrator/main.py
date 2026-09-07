"""Entry point. ``python -m orchestrator.main`` or ``orchestrator`` (installed)."""

from __future__ import annotations

import uvicorn

from orchestrator.config import get_settings


def cli() -> None:
    settings = get_settings()
    uvicorn.run(
        "orchestrator.web.app:app",
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,  # let structlog own stdout
        access_log=False,
    )


if __name__ == "__main__":
    cli()
