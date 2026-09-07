"""FastAPI application factory + lifespan wiring.

Lifespan tasks in order:
    1. configure logging
    2. build DB engine
    3. (optional) alembic upgrade head
    4. reconcile services.yaml into the DB (YAML-wins boot rule)
    5. build + start adapters
Shutdown reverses 5.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator import __version__
from orchestrator.adapters.factory import build_adapters
from orchestrator.config import get_settings
from orchestrator.db.session import build_engine, session_scope
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.web.routers import backup, dashboard, health, infra, maintenance, wake


def _maybe_run_migrations() -> None:
    """Run alembic upgrade head only if configured. Import lazily so tests
    that create tables via ``SQLModel.metadata.create_all`` skip alembic."""
    settings = get_settings()
    if not settings.run_migrations_on_start:
        return

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log = get_logger(__name__)
    settings = get_settings()

    log.info(
        "orchestrator.start",
        version=__version__,
        env=settings.orchestrator_env,
        dry_run=settings.dry_run,
    )

    build_engine(settings)
    try:
        _maybe_run_migrations()
    except Exception as exc:
        # Don't crash the maintenance responder if migrations fail — surface it.
        log.error("migrations.failed", error=str(exc))

    # YAML-wins boot reconcile — only if the file is present.
    if settings.services_yaml_path.exists():
        try:
            with session_scope() as session:
                reconcile_yaml_into_db(session, settings.services_yaml_path)
        except Exception as exc:
            log.error(
                "registry.reconcile.failed",
                path=str(settings.services_yaml_path),
                error=str(exc),
            )
    else:
        log.warning("registry.yaml.missing", path=str(settings.services_yaml_path))

    adapters = build_adapters(settings)
    await adapters.start_all()
    app.state.adapters = adapters

    try:
        yield
    finally:
        log.info("orchestrator.stop")
        await adapters.stop_all()


def create_app() -> FastAPI:
    app = FastAPI(
        title="homelab-orchestrator",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(maintenance.router)
    app.include_router(wake.router)
    app.include_router(dashboard.router)
    app.include_router(infra.router)
    app.include_router(backup.router)
    return app


app = create_app()
