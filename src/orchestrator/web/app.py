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
    9. start the access-log collector (if ACCESS_LOG_ENABLED and the proxy
       adapter is live; also writes GeoIP samples when GEOIP_ENABLED)
   10. start service IP discovery (if IP_DISCOVERY_ENABLED)
Shutdown reverses 10 then 9 then 8 then 7 then 6 then 5.
"""

from __future__ import annotations

import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from orchestrator import __version__
from orchestrator.adapters.factory import build_adapters
from orchestrator.config import get_settings
from orchestrator.db.session import build_engine, session_scope
from orchestrator.events import install_db_hooks
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.notify import hooks as notify_hooks
from orchestrator.notify.engine import NotificationEngine
from orchestrator.registry.loader import RegistryPathError, reconcile_yaml_into_db
from orchestrator.scheduler import BackupScheduler
from orchestrator.scheduler.access_log_collector import AccessLogCollector
from orchestrator.scheduler.health_scanner import HealthScanner
from orchestrator.scheduler.ip_discovery import IpDiscoveryScheduler
from orchestrator.scheduler.self_backup import SelfBackupScheduler
from orchestrator.scheduler.traffic_collector import TrafficCollector
from orchestrator.system.selfupdate import announce_last_result as announce_last_update
from orchestrator.web.routers import (
    actions,
    activity,
    approvals,
    assistants,
    auth,
    backup,
    backup_policies,
    bulk,
    dashboard,
    deploys,
    display,
    dumps,
    events,
    geo_traffic,
    health,
    hooks,
    infra,
    linked,
    maintenance,
    mcp_endpoint,
    mcp_oauth,
    notifications,
    portainer,
    proxy_hosts,
    push,
    setup,
    stacks,
    system,
    traffic,
    ui,
    wake,
)
from orchestrator.web.routers import (
    map as map_page,
)
from orchestrator.web.static_files import RevalidatingStaticFiles


def _maybe_run_migrations() -> None:
    """Run alembic upgrade head only if configured. Import lazily so tests
    that create tables via ``SQLModel.metadata.create_all`` skip alembic."""
    settings = get_settings()
    if not settings.run_migrations_on_start:
        return

    from alembic import command
    from alembic.config import Config

    from orchestrator.system.backups import BackupError, create_backup
    from orchestrator.system.info import schema_status

    # A database that is about to change shape gets a copy first. Fail-open: a
    # backup that cannot be taken is logged, the migration still runs.
    try:
        status = schema_status(settings)
        if status["current"] is not None and status["pending"]:
            info = create_backup(settings, reason="pre-migration")
            get_logger(__name__).info("migrations.backup", name=info.name, to=status["head"])
    except (BackupError, OSError) as exc:
        get_logger(__name__).warning("migrations.backup_skipped", error=str(exc))

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log = get_logger(__name__)
    settings = get_settings()
    install_db_hooks()  # committed writes -> live-update events (orchestrator/events.py)

    log.info(
        "orchestrator.start",
        version=__version__,
        env=settings.orchestrator_env,
        dry_run=settings.dry_run,
    )

    # A restore staged from the dashboard is applied here, before the database
    # is opened, never under a running app.
    try:
        from orchestrator.system.backups import apply_pending_restore

        restored = apply_pending_restore(settings)
        if restored:
            log.warning("system.restored", backup=restored)
    except Exception as exc:
        log.error("system.restore.failed", error=str(exc))

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
    # One engine for the scanner, the recovery loop and the API, so they share
    # the same delivery and the same feed.
    notify_engine = NotificationEngine(adapters.notifier, settings)
    app.state.notifications = notify_engine
    notify_hooks.install(notify_engine)
    # Tell people how the last self-update went (an update that rolled back included).
    await announce_last_update()

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
        health_scanner = HealthScanner(
            settings=settings, adapters=adapters, notifications=notify_engine
        )
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

    # Same resilience rule as every other scheduler: a failure to start
    # must not take the app down. One access-log tail feeds both /traffic
    # (per-minute rollups) and /traffic/geo (GeoIP samples, GEOIP_ENABLED
    # only) — see docs/traffic_metrics.md.
    access_log_collector: AccessLogCollector | None = None
    try:
        access_log_collector = AccessLogCollector(settings=settings, adapters=adapters)
        access_log_collector.start()
    except Exception as exc:
        log.error(
            "access_log_collector.start.failed",
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(),
            impact="No access-log traffic will be collected; /traffic and /traffic/geo stay empty.",
        )
        access_log_collector = None
    app.state.access_log_collector = access_log_collector

    # Same resilience rule again: IP discovery is a convenience (proxy
    # upstreams, ping probes), so a failure to start is logged, not fatal.
    ip_discovery: IpDiscoveryScheduler | None = None
    try:
        ip_discovery = IpDiscoveryScheduler(settings=settings, adapters=adapters)
        ip_discovery.start()
    except Exception as exc:
        log.error(
            "ip_discovery.start.failed",
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(),
            impact="Service IPs are only discovered on a manual 'Discover now'.",
        )
        ip_discovery = None
    app.state.ip_discovery = ip_discovery

    # Backups of our own database + registry. Resilience rule as above.
    self_backup: SelfBackupScheduler | None = None
    try:
        self_backup = SelfBackupScheduler(settings=settings)
        self_backup.start()
    except Exception as exc:
        log.error(
            "self_backup.start.failed",
            error=str(exc),
            error_type=type(exc).__name__,
            impact="No scheduled self-backups; manual ones on /system still work.",
        )
        self_backup = None
    app.state.self_backup = self_backup

    try:
        yield
    finally:
        log.info("orchestrator.stop")
        notify_hooks.install(None)
        if self_backup is not None:
            await self_backup.shutdown()
        if ip_discovery is not None:
            await ip_discovery.shutdown()
        if access_log_collector is not None:
            await access_log_collector.shutdown()
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
        "/static",
        RevalidatingStaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
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
    app.include_router(proxy_hosts.catalog_router)
    app.include_router(bulk.router)
    app.include_router(actions.router)
    app.include_router(linked.router)
    app.include_router(deploys.router)
    app.include_router(dumps.router)
    app.include_router(hooks.router)
    app.include_router(events.router)
    app.include_router(stacks.router)
    app.include_router(approvals.router)
    app.include_router(assistants.router)
    app.include_router(mcp_endpoint.router)
    app.include_router(mcp_oauth.router)
    app.include_router(system.router)
    app.include_router(display.router)
    app.include_router(notifications.router)
    app.include_router(portainer.router)
    app.include_router(push.router)
    app.include_router(map_page.router)
    app.include_router(activity.router)
    app.include_router(geo_traffic.router)
    app.include_router(traffic.router)
    # Unauthenticated only while no admin exists AND nothing is configured —
    # see the gating notes at the top of routers/setup.py.
    app.include_router(setup.router)
    return app


app = create_app()
