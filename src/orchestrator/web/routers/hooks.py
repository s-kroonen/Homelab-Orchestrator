"""The deploy webhook endpoint (public, authenticated by the per-service secret) and the
admin endpoints that create and rotate that secret."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import desc, select

from orchestrator.audit import log as audit
from orchestrator.db.models import PipelineRun, Service
from orchestrator.deploy import webhook as hook
from orchestrator.deploy.pipeline import DeployPipeline, DeployRefused, Trigger
from orchestrator.deploy.policy import is_enabled, webhook_of
from orchestrator.domain.enums import PipelineKind
from orchestrator.logging_setup import get_logger
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.routers.deploys import refusal, run_view

log = get_logger(__name__)

public = APIRouter(tags=["deploy-webhook"])
admin = APIRouter(
    prefix="/api/services", tags=["deploy-webhook"], dependencies=[Depends(require_admin)]
)
router = APIRouter()
router.include_router(public)
router.include_router(admin)


@public.post("/hooks/deploy/{slug}", status_code=status.HTTP_202_ACCEPTED)
async def deploy_webhook(
    slug: str,
    request: Request,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Deploy ``slug`` - guarded - when a verified push arrives. A service without a webhook
    answers 404 whether or not it exists, so this cannot be used to list services."""
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    cfg = webhook_of(svc.deploy) if svc is not None else None
    if svc is None or cfg is None or not is_enabled(svc.deploy):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such webhook")

    body = await request.body()
    try:
        payload = json.loads(body) if body.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}
    try:
        delivery = hook.verify(
            session,
            slug,
            cfg["provider"],
            headers={k.lower(): v for k, v in request.headers.items()},
            query=dict(request.query_params),
            body=body,
            payload=payload,
        )
    except hook.WebhookRejected as exc:
        log.warning("deploy.webhook_rejected", service=slug, reason=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised") from None

    if not delivery.deploys:
        return {"service": slug, "started": False, "ignored": delivery.note}
    if not hook.branch_matches(cfg.get("branch", ""), delivery.ref):
        return {
            "service": slug,
            "started": False,
            "ignored": f"{delivery.ref} is not the branch this deploys ({cfg['branch']})",
        }

    pipeline = DeployPipeline(adapters, settings)
    trigger = Trigger("webhook", f"webhook:{delivery.provider}", delivery.as_detail())
    try:
        run = pipeline.start(session, slug, trigger)
    except DeployRefused as exc:
        audit.record(
            session,
            actor=trigger.actor,
            action="deploy.webhook_refused",
            target=f"service:{slug}",
            details={"reason": str(exc), "code": exc.code, **delivery.as_detail()},
        )
        session.commit()
        raise refusal(exc) from exc
    assert run.id is not None
    pipeline.fire(run.id, slug, trigger)
    return {"started": True, **run_view(run, slug)}


@public.get("/hooks/deploy/{slug}/runs/{run_id}")
async def deploy_webhook_run(
    slug: str, run_id: int, request: Request, session: SessionDep
) -> dict[str, Any]:
    """How a deploy this service's webhook started is going, for the caller (a CI job) that
    waits for it. Authenticated by the same key; shows only step names, results and reasons -
    and only for a deploy of this service."""
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None or webhook_of(svc.deploy) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such webhook")
    try:
        hook.authenticate(
            session,
            slug,
            headers={k.lower(): v for k, v in request.headers.items()},
            query=dict(request.query_params),
        )
    except hook.WebhookRejected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised") from None
    run = session.get(PipelineRun, run_id)
    if run is None or run.kind is not PipelineKind.DEPLOY or run.service_id != svc.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such deploy")
    return {
        "run_id": run.id,
        "status": run.status.value,
        "error": run.error or "",
        "steps": [
            {
                "name": st["name"],
                "status": st["status"],
                "message": str((st.get("detail") or {}).get("error") or "")[:300],
            }
            for st in run.steps
        ],
    }


# -- admin: the secret, and a summary for the Deploy panel ---------------------------------------


def _public(url: str) -> str:
    """The webhook URL, or a placeholder when no public address is configured."""
    return f"https://YOUR-ORCHESTRATOR{url}" if url.startswith("/") else url


def _service(session: SessionDep, slug: str) -> Service:
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    return svc


@admin.get("/{slug}/deploy")
async def deploy_summary(slug: str, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    """Deploy settings, whether the webhook has a secret, and the last deploy."""
    svc = _service(session, slug)
    cfg = webhook_of(svc.deploy)
    last = session.exec(
        select(PipelineRun)
        .where(PipelineRun.service_id == svc.id, PipelineRun.kind == PipelineKind.DEPLOY)
        .order_by(desc(PipelineRun.started_at))
        .limit(1)
    ).first()
    url = hook.webhook_url(settings, slug)
    return {
        "slug": slug,
        "deploy": dict(svc.deploy or {}),
        "webhook": {
            "configured": cfg is not None,
            "provider": cfg["provider"] if cfg else None,
            "branch": cfg["branch"] if cfg else None,
            "secret_set": hook.has_secret(session, slug),
            "url": url if cfg else None,
            "setup": hook.setup_hint(cfg["provider"], url) if cfg else [],
            "github": hook.github_workflow(slug, _public(url), cfg["branch"]) if cfg else None,
            "url_is_public": not url.startswith("/"),
        },
        "last": run_view(last, slug) if last else None,
    }


@admin.post("/{slug}/deploy/webhook")
async def create_webhook_secret(
    slug: str, session: SessionDep, settings: SettingsDep, who: CurrentAdmin
) -> dict[str, Any]:
    """Make (or replace) the webhook secret. It is in this response and nowhere else, ever."""
    svc = _service(session, slug)
    cfg = webhook_of(svc.deploy)
    if cfg is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this service's deploy settings do not accept a webhook yet - turn one on first",
        )
    secret = hook.rotate_secret(session, slug)
    audit.record(
        session, actor=who.username, action="deploy.webhook_secret", target=f"service:{slug}"
    )
    session.commit()
    url = hook.webhook_url(settings, slug)
    return {
        "slug": slug,
        "provider": cfg["provider"],
        "secret": secret,
        "url": url,
        "setup": hook.setup_hint(cfg["provider"], url),
        "github": hook.github_workflow(slug, _public(url), cfg["branch"]),
        # the same workflow with the key written into it - only possible right now, and not
        # something to commit to a repository
        "github_inline_key": hook.github_workflow(slug, _public(url), cfg["branch"], key=secret)[
            "workflow"
        ],
        "note": "Copy the secret now: it cannot be shown again, only replaced.",
    }


@admin.delete("/{slug}/deploy/webhook")
async def delete_webhook_secret(
    slug: str, session: SessionDep, who: CurrentAdmin
) -> dict[str, Any]:
    _service(session, slug)
    removed = hook.remove_secret(session, slug)
    audit.record(
        session,
        actor=who.username,
        action="deploy.webhook_secret_removed",
        target=f"service:{slug}",
    )
    session.commit()
    return {"slug": slug, "removed": removed}
