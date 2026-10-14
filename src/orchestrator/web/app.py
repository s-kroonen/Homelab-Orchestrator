"""FastAPI application factory + lifespan wiring.

Lifespan tasks in order:
    1. configure logging
    2. build DB engine
    3. (optional) alembic upgrade head
    4. reconcile services.yaml into the DB (YAML-wins boot rule)
    5. build + start adapters
    6. start the backup scheduler (if SCHEDULER_ENABLED)
    7. start the traffic collector (if TRAEFIK_API_URL is set)
    8. start the health scanner (if HEALTH_SCAN_ENABLED)
Shutdown reverses 8 then 7 then 6 then 5.
"""

from __future__ import annotations

import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from orchestrator import __version__
from orchestrator.adapters.factory import build_adapters
from orchestrator.config import get_settings
from orchestrator.db.session import build_engine, session_scope
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.registry.loader import RegistryPathError, reconcile_yaml_into_db
from orchestrator.scheduler import BackupScheduler
from orchestrator.scheduler.health_scanner import HealthScanner
from orchestrator.scheduler.traffic_collector import TrafficCollector
from orchestrator.web.routers import (
    activity,
    auth,
    backup,
    backup_policies,
    dashboard,
    health,
    infra,
    maintenance,
    proxy_hosts,
    setup,
    ui,
    wake,
)


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

    # YAML-wins boot reconcile. A bad registry path is a configuration error,
    # not a transient one — surface it with an actionable message. We still do
    # not crash: the maintenance responder must stay up to serve a fallback
    # page even when the registry is unusable (spec section 2, fail-open).
    try:
        with session_scope() as session:
            reconcile_yaml_into_db(session, settings.services_yaml_path)
    except RegistryPathError as exc:
        log.error(
            "registry.path.invalid",
            path=str(settings.services_yaml_path),
            error=str(exc),
            impact="No services are registered. Backups and wakes cannot run "
            "until this is fixed.",
        )
    except Exception as exc:
        log.error(
            "registry.reconcile.failed",
            path=str(settings.services_yaml_path),
            error=str(exc),
            impact="No services are registered. Backups and wakes cannot run "
            "until this is fixed.",
        )

    # Startup failures past this point must never be silent. uvicorn runs with
    # log_config=None so structlog owns stdout, which means an unhandled
    # exception here would otherwise vanish and the container would just
    # crash-loop with no explanation.
    try:
        adapters = build_adapters(settings)
        await adapters.start_all()
    except Exception as exc:
        log.error(
            "orchestrator.startup.failed",
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(),
            hint="The process will exit. Fix the cause above; a container with "
            "restart:unless-stopped will otherwise loop.",
        )
        raise

    app.state.adapters = adapters

    # A scheduler that fails to start is logged, not fatal: the maintenance
    # responder and manual backups must stay up, and the dashboard shows the
    # scheduler as not running.
    scheduler: BackupScheduler | None = None
    if settings.scheduler_enabled:
        try:
            scheduler = BackupScheduler(adapters=adapters, settings=settings)
            scheduler.start()
        except Exception as exc:
            log.error(
                "scheduler.start.failed",
                error=str(exc),
                error_type=type(exc).__name__,
                traceback=traceback.format_exc(),
                impact="No scheduled backups will run. Manual backups still work.",
            )
            scheduler = None
    else:
        log.warning("scheduler.disabled", impact="SCHEDULER_ENABLED=false — no backups run.")
    app.state.scheduler = scheduler

    # Same resilience rule as the backup scheduler: a failure to start must
    # not take the app down, and this feature is optional in the first place
    # (TRAEFIK_API_URL unset -> TrafficCollector.start() is a no-op).
    traffic_collector: TrafficCollector | None = None
    try:
        traffic_collector = TrafficCollector(settings=settings, adapters=adapters)
        traffic_collector.start()
    except Exception as exc:
        log.error(
            "traffic_collector.start.failed",
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(),
            impact="No traffic metrics will be collected. /traffic will show no data.",
        )
        traffic_collector = None
    app.state.traffic_collector = traffic_collector

    # Same resilience rule as the other two schedulers: a failure to start
    # must not take the app down, and HEALTH_SCAN_ENABLED=false makes this
    # one an intentional no-op too.
    health_scanner: HealthScanner | None = None
    try:
        health_scanner = HealthScanner(settings=settings)
        health_scanner.start()
    except Exception as exc:
        log.error(
            "health_scanner.start.failed",
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(),
            impact="ServiceState will only update as a side effect of a wake "
            "or a backup's gate check — not on a schedule.",
        )
        health_scanner = None
    app.state.health_scanner = health_scanner

    try:
        yield
    finally:
        log.info("orchestrator.stop")
        if health_scanner is not None:
            await health_scanner.shutdown()
        if traffic_collector is not None:
            await traffic_collector.shutdown()
        if scheduler is not None:
            await scheduler.shutdown()
        await adapters.stop_all()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="homelab-orchestrator",
        version=__version__,
        lifespan=lifespan,
    )
    # Signed-cookie session for the dashboard's passkey login (web/auth.py).
    # https_only=False: Traefik terminates TLS and speaks plain HTTP to this
    # container internally (see docs/dashboard_auth.md) — the cookie itself
    # never crosses the network in the clear on a correctly configured
    # deployment, but Starlette can't see past its own TLS-terminated hop.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        same_site="lax",
        https_only=False,
    )
    app.mount(
        "/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"
    )
    app.include_router(health.router)
    app.include_router(maintenance.router)
    app.include_router(wake.router)
    app.include_router(auth.router)
    app.include_router(ui.router)
    app.include_router(dashboard.router)
    app.include_router(infra.router)
    app.include_router(backup.router)
    app.include_router(backup_policies.router)
    app.include_router(proxy_hosts.router)
    app.include_router(activity.router)
    # Unauthenticated only while no admin exists AND nothing is configured —
    # see the gating notes at the top of routers/setup.py.
    app.include_router(setup.router)
    return app


app = create_app()
