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

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from orchestrator.adapters.errors import AdapterError
from orchestrator.db.models import Service
from orchestrator.domain.enums import PipelineStatus
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.wake import WakeError, WakePipeline
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.wake_support import fire_background

router = APIRouter(tags=["maintenance"])
log = get_logger(__name__)

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

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

    if not service.enabled:
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

    pipeline = WakePipeline(power=adapters.power, proxmox=adapters.proxmox, settings=settings)
    try:
        run, started_new = pipeline.get_or_start(session, service_slug, actor="maintenance-page")
        if started_new:
            fire_background(pipeline, run, service_slug, actor="maintenance-page")
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
