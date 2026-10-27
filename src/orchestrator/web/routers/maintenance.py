"""Maintenance / status page.

Traefik's ``errors`` middleware sends a visitor here when their backend
returns a bad status — see docs/gateway_traefik_migration.md for wiring it up
and docs/architecture.md for why NPM can't do this. This renders a
self-refreshing "starting up" page and fires the wake pipeline; once the
service reports HEALTHY, the visitor's next reload reaches the real backend
(Traefik does not rewrite their URL — the errors middleware serves this
page's body under the domain the visitor is already on).

Fail-open rule: this responder must render a page even when things it reads
are having a bad day — a 5xx from here would replace the last usable signal
an operator (or a confused visitor) has with a blank error. The pipeline
logic below is wrapped accordingly; a genuinely unreachable database is the
one failure this can't paper over (FastAPI's own session dependency raises
before the handler body runs) — see ``/readyz`` for that same limit.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, desc, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.db.models import Node, PipelineRun, Service
from orchestrator.domain.enums import PipelineKind, PipelineStatus, WakeTrigger
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.wake import (
    WakeError,
    WakePipeline,
    WakeUnsupportedError,
    wake_is_unsupported,
)
from orchestrator.registry.tree import is_served
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.templating import templates
from orchestrator.web.wake_button import WAKE_HEADER, make_token, verify_token
from orchestrator.web.wake_support import fire_background

router = APIRouter(tags=["maintenance"])
log = get_logger(__name__)

_templates = templates

_FALLBACK_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5"><title>Starting up</title></head>
<body style="font-family:sans-serif;text-align:center;padding:4rem 1rem;">
<h1>Starting up&hellip;</h1><p>This page will refresh automatically.</p>
</body></html>"""


def _render(request: Request, **context: object) -> HTMLResponse:
    try:
        return _templates.TemplateResponse(request, "maintenance.html", context)
    except Exception:
        log.exception("maintenance.render_failed")
        return HTMLResponse(_FALLBACK_HTML)


def wake_disabled_reason(service: Service, node: Node | None) -> str | None:
    """Why a visitor's request must NOT start this service (``None`` = it may).
    The maintenance page is the unattended, visitor-triggered wake, so it obeys
    the same per-service and per-node "wake on hit" switches as the traffic
    collector; the manual button, the CLI and ``POST /wake`` do not."""
    if not service.wake_on_hit_enabled:
        return f"Automatic start is turned off for {service.name}."
    if node is not None and not node.wake_on_hit_enabled:
        return f"Automatic start is turned off for {node.name}, the host that runs {service.name}."
    return None


def _active_wake_run(session: Session, service: Service) -> PipelineRun | None:
    """A wake already in flight (started by an operator, say). Joining it is
    always fine; only STARTING one is gated."""
    run = session.exec(
        select(PipelineRun)
        .where(PipelineRun.service_id == service.id, PipelineRun.kind == PipelineKind.WAKE)
        .order_by(desc(PipelineRun.started_at))
        .limit(1)
    ).one_or_none()
    if run is not None and run.status in (PipelineStatus.QUEUED, PipelineStatus.RUNNING):
        return run
    return None


@router.get("/maintenance/{service_slug}")
async def maintenance_page(
    service_slug: str,
    request: Request,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
) -> HTMLResponse:
    service = session.exec(select(Service).where(Service.slug == service_slug)).one_or_none()
    if service is None:
        return _render(
            request,
            service_name=service_slug,
            headline="Unknown service",
            reason=(
                f"{service_slug!r} is not in the orchestrator's registry. If this is a real "
                f"service, check the slug in its Traefik dynamic-config file."
            ),
            spinner_class="failed",
            steps=[],
            refresh_seconds=None,
            show_retry=False,
        )

    if not is_served(session, service):
        return _render(
            request,
            service_name=service.name,
            headline="Not managed",
            reason=f"{service.name} is disabled in the orchestrator and will not be auto-started.",
            spinner_class="failed",
            steps=[],
            refresh_seconds=None,
            show_retry=False,
        )

    # Checked BEFORE even attempting a wake, and recomputed on every page
    # load rather than inferred from a stored run's error string — see
    # orchestrator.pipelines.wake.wake_is_unsupported. This matters beyond
    # the very first hit: get_or_start() below reuses a prior run (RUNNING,
    # or within WAKE_RETRY_COOLDOWN_S) WITHOUT re-raising on every reload, so
    # relying solely on catching WakeUnsupportedError from get_or_start would
    # only show this page on the first visitor's first hit and fall back to
    # the generic "Could not start" page for everyone after.
    node = session.get(Node, service.node_id) if service.node_id else None
    if wake_is_unsupported(node):
        return _render(
            request,
            service_name=service.name,
            headline=f"{service.name} cannot be started automatically",
            reason=(
                f"{service.name} has no power management configured — this host has no "
                f"way to be turned on remotely. Retrying will not help; contact an "
                f"administrator."
            ),
            spinner_class="unsupported",
            steps=[],
            refresh_seconds=None,
            show_retry=False,
        )

    pipeline = WakePipeline(power=adapters.power, proxmox=adapters.proxmox, settings=settings)
    run: PipelineRun | None = None
    disabled = wake_disabled_reason(service, node)
    if disabled is not None:
        run = _active_wake_run(session, service)
        if run is None:
            return _render(
                request,
                service_name=service.name,
                headline=f"{service.name} is not available",
                reason=f"{disabled} It will not be started by visiting this page. "
                f"Contact an administrator.",
                spinner_class="unsupported",
                steps=[],
                refresh_seconds=None,
                show_retry=False,
            )
    secret = settings.session_secret.get_secret_value()
    if (
        run is None
        and service.wake_trigger is WakeTrigger.BUTTON
        and not verify_token(secret, service_slug, request.headers.get(WAKE_HEADER))
    ):
        # Not pressed yet: join a wake already running, otherwise offer the
        # button and start nothing - a plain request never wakes this service.
        run = _active_wake_run(session, service)
        if run is None:
            return _render(
                request,
                service_name=service.name,
                headline=f"{service.name} is offline",
                reason=f"{service.name} is not running right now. Press the button to start it; "
                f"this can take a minute or two.",
                spinner_class="unsupported",
                steps=[],
                refresh_seconds=None,
                show_retry=False,
                wake_token=make_token(secret, service_slug),
                wake_header=WAKE_HEADER,
            )
    try:
        if run is None:
            run, started_new = pipeline.get_or_start(
                session, service_slug, actor="maintenance-page"
            )
            if started_new:
                fire_background(pipeline, run, service_slug, actor="maintenance-page")
    except WakeUnsupportedError as exc:
        # Defense in depth: the early check above should always catch this
        # first, but node state could theoretically change between that
        # check and here. Same distinct rendering either way.
        log.error("maintenance.wake_unsupported", service=service_slug, error=str(exc))
        return _render(
            request,
            service_name=service.name,
            headline=f"{service.name} cannot be started automatically",
            reason=(
                f"{service.name} has no power management configured — this host has no "
                f"way to be turned on remotely. Retrying will not help; contact an "
                f"administrator."
            ),
            spinner_class="unsupported",
            steps=[],
            refresh_seconds=None,
            show_retry=False,
        )
    except (WakeError, AdapterError) as exc:
        # Couldn't even start — still fail-open, still no 5xx.
        log.error("maintenance.wake_trigger_failed", service=service_slug, error=str(exc))
        return _render(
            request,
            service_name=service.name,
            headline=f"Could not start {service.name}",
            reason=str(exc),
            spinner_class="failed",
            steps=[],
            refresh_seconds=None,
            show_retry=True,
            retry_url="/",
        )

    if run.status is PipelineStatus.SUCCEEDED:
        return _render(
            request,
            service_name=service.name,
            headline="Ready",
            reason=f"{service.name} is healthy. Try your request again.",
            spinner_class="done",
            steps=run.steps,
            refresh_seconds=None,
            show_retry=True,
            retry_url="/",
        )

    if run.status is PipelineStatus.FAILED:
        return _render(
            request,
            service_name=service.name,
            headline=f"Could not start {service.name}",
            reason=run.error or "The wake pipeline failed for an unknown reason.",
            spinner_class="failed",
            steps=run.steps,
            refresh_seconds=None,
            show_retry=True,
            retry_url="/",
        )

    # RUNNING (or, briefly, QUEUED) — still in progress.
    last_step = run.steps[-1]["name"].replace("_", " ") if run.steps else "starting"
    return _render(
        request,
        service_name=service.name,
        headline=f"Waking {service.name}…",
        reason=f"Current step: {last_step}. This page refreshes automatically.",
        spinner_class="",
        steps=run.steps,
        refresh_seconds=settings.wake_poll_interval_s if settings.wake_poll_interval_s >= 2 else 5,
        show_retry=False,
    )
