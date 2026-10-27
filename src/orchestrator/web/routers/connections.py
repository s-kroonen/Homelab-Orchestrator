"""Connections: the adapters and controllers the orchestrator talks through, edited from the
dashboard (``connections.yaml``, applied without a restart). The operations are in
``orchestrator.connections.service``; ``orchestrator-cli connections`` calls the same ones.
See docs/connections.md.

Secrets are never returned: a connection reports which secret fields are set. Admin only; each
change is audited (the fields that changed, never their values).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from orchestrator.audit import log as audit
from orchestrator.config import Settings, reload_settings
from orchestrator.connections import runtime, service
from orchestrator.connections.checks import check_connection
from orchestrator.connections.kinds import KINDS
from orchestrator.connections.model import Connection, Scope
from orchestrator.connections.store import path_for
from orchestrator.db.models import Service
from orchestrator.web.auth import CurrentAdmin, get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

api = APIRouter(
    prefix="/api/connections", tags=["connections"], dependencies=[Depends(require_admin)]
)
pages = APIRouter(tags=["connections"])
router = APIRouter()
router.include_router(api)
router.include_router(pages)

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


class ConnectionIn(BaseModel):
    kind: str
    id: str = ""
    name: str = ""
    enabled: bool = True
    default: bool = False
    site: str = ""
    scope: Scope = "local"
    notes: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class ConnectionPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    site: str | None = None
    scope: Scope | None = None
    notes: str | None = None
    settings: dict[str, Any] | None = None
    #: A blank value keeps the stored secret; to remove one, list its field in ``clear_secrets``.
    secrets: dict[str, str] | None = None
    clear_secrets: list[str] | None = None


class TestIn(BaseModel):
    #: Test what is in the form: an existing connection's id whose stored secrets fill the blanks.
    existing_id: str = ""
    kind: str
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class ImportIn(BaseModel):
    body: str


def _refuse(exc: service.Refused) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _audit(session: Session, admin: Any, action: str, target: str, **details: Any) -> None:
    audit.record(session, actor=admin.username, action=action, target=target, details=details)
    session.commit()


async def _apply(settings: Settings, adapters: AdaptersDep) -> None:
    """Put the file in force: settings first, then the adapters built from them."""
    current = reload_settings()
    await adapters.reload(current)


def _kinds() -> list[dict[str, Any]]:
    return [
        {
            "kind": s.kind,
            "title": s.title,
            "blurb": s.blurb,
            "multi": s.multi,
            "channel": s.channel,
            "marker": s.marker,
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.type,
                    "required": f.required,
                    "help": f.help,
                    "choices": list(f.choices),
                    "default": "" if f.is_secret else f.default,
                }
                for f in s.fields
            ],
        }
        for s in KINDS
    ]


def _view(settings: Settings) -> dict[str, Any]:
    snap = runtime.current()
    path = snap.path or path_for(settings)
    return {
        "path": str(path),
        "exists": path.is_file(),
        "error": snap.error,
        "site": settings.orchestrator_site,
        "kinds": _kinds(),
        "connections": [c.public() for c in snap.all],
        "env_keys": service.env_keys_in_use(settings),
    }


@pages.get("/connections", response_model=None)
async def connections_page(
    request: Request, session: SessionDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    return _templates.TemplateResponse(request, "connections.html", {"admin": admin})


@api.get("")
async def list_connections(settings: SettingsDep) -> dict[str, Any]:
    return _view(settings)


@api.post("", status_code=201)
async def create_connection(
    body: ConnectionIn,
    settings: SettingsDep,
    adapters: AdaptersDep,
    session: SessionDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    try:
        change = service.create(
            settings,
            kind=body.kind,
            ident=body.id,
            name=body.name,
            enabled=body.enabled,
            default=body.default,
            site=body.site,
            scope=body.scope,
            notes=body.notes,
            settings_in=body.settings,
            secrets_in=body.secrets,
        )
    except service.Refused as exc:
        raise _refuse(exc) from exc
    conn = change.connection
    assert conn is not None
    _audit(
        session,
        admin,
        "connection.created",
        f"connection:{conn.id}",
        kind=conn.kind,
        fields=sorted({*body.settings, *body.secrets}),
    )
    await _apply(settings, adapters)
    return _view(settings) | {"id": conn.id}


@api.put("/{ident}")
async def update_connection(
    ident: str,
    body: ConnectionPatch,
    settings: SettingsDep,
    adapters: AdaptersDep,
    session: SessionDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    try:
        change = service.update(
            settings,
            ident,
            name=body.name,
            enabled=body.enabled,
            site=body.site,
            scope=body.scope,
            notes=body.notes,
            settings_in=body.settings,
            secrets_in=body.secrets,
            clear_secrets=body.clear_secrets,
        )
    except service.Refused as exc:
        raise _refuse(exc) from exc
    changed = sorted(
        {
            *(body.settings or {}),
            *(k for k, v in (body.secrets or {}).items() if v),
            *(body.clear_secrets or []),
        }
    )
    _audit(session, admin, "connection.updated", f"connection:{ident}", fields=changed)
    await _apply(settings, adapters)
    assert change.connection is not None
    return _view(settings)


@api.delete("/{ident}")
async def delete_connection(
    ident: str,
    settings: SettingsDep,
    adapters: AdaptersDep,
    session: SessionDep,
    admin: CurrentAdmin,
    force: bool = False,
) -> dict[str, Any]:
    try:
        service.delete(session, settings, ident, force=force)
    except service.Refused as exc:
        raise _refuse(exc) from exc
    _audit(session, admin, "connection.deleted", f"connection:{ident}", forced=force)
    await _apply(settings, adapters)
    return _view(settings)


@api.post("/{ident}/default")
async def make_default(
    ident: str,
    settings: SettingsDep,
    adapters: AdaptersDep,
    session: SessionDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    try:
        change = service.set_default(session, settings, ident)
    except service.Refused as exc:
        raise _refuse(exc) from exc
    _audit(session, admin, "connection.default", f"connection:{ident}", pinned=change.pinned)
    await _apply(settings, adapters)
    registry_dirty = change.registry_dirty
    if registry_dirty:  # the links just pinned exist only in the database until saved
        from orchestrator.registry.loader import compute_registry_diff

        registry_dirty = compute_registry_diff(session).dirty
    return _view(settings) | {"pinned": change.pinned, "registry_dirty": registry_dirty}


@api.post("/test")
async def test_candidate(body: TestIn, settings: SettingsDep) -> dict[str, Any]:
    """Look at a connection as the form has it, before it is saved."""
    stored = runtime.current().get(body.existing_id) if body.existing_id else None
    # not enabled: an incomplete form is answered by the check ("... is not filled in"), not refused
    conn = Connection(id="candidate", kind=body.kind, enabled=False, settings=dict(body.settings))
    conn.secrets = dict(stored.secrets) if stored is not None and stored.kind == body.kind else {}
    conn.secrets |= {k: v for k, v in body.secrets.items() if v}
    try:
        conn.validate_values()
    except ValueError as exc:  # ConnectionError, or a kind that does not exist
        raise HTTPException(422, str(exc)) from exc
    return (await check_connection(conn, settings)).as_dict()


@api.post("/{ident}/test")
async def test_saved(ident: str, settings: SettingsDep) -> dict[str, Any]:
    conn = runtime.current().get(ident)
    if conn is None:
        raise HTTPException(404, f"no connection called {ident!r}")
    return (await check_connection(conn, settings)).as_dict()


@api.post("/import-env")
async def import_env(
    settings: SettingsDep, adapters: AdaptersDep, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    """Write what ``.env`` configures into the file. The keys it listed can then be deleted from
    ``.env`` (they are ignored from now on); this container cannot edit that file itself."""
    try:
        moved, keys = service.import_env(settings)
    except service.Refused as exc:
        raise _refuse(exc) from exc
    _audit(session, admin, "connection.import_env", "connections", moved=[c.id for c in moved])
    await _apply(settings, adapters)
    return _view(settings) | {"moved": [c.id for c in moved], "delete_from_env": keys}


@api.post("/reload")
async def reload_connections(settings: SettingsDep, adapters: AdaptersDep) -> dict[str, Any]:
    """Re-read the file (after it was edited on disk) and rebuild the adapters."""
    await _apply(settings, adapters)
    return _view(settings)


@api.get("/controllers")
async def controllers(session: SessionDep, settings: SettingsDep) -> list[dict[str, Any]]:
    """Every service with a controller, and the connection it acts through."""
    snap = runtime.current()
    out: list[dict[str, Any]] = []
    for svc in session.exec(select(Service).order_by(Service.slug)).all():  # type: ignore[arg-type]
        cfg = svc.controller or {}
        ctype = str(cfg.get("type") or "")
        if not ctype:
            continue
        ident, conn = "", None
        if ctype == "portainer":
            ident = runtime.instance_of(cfg, settings)
            conn = snap.get(ident)
        out.append(
            {
                "slug": svc.slug,
                "name": svc.name,
                "type": ctype,
                "connection": ident,
                "connection_name": conn.label if conn else "",
                "usable": bool(conn and conn.enabled and conn.configured) if ident else True,
            }
        )
    return out


@api.get("/export", response_class=PlainTextResponse)
async def export_shared(
    settings: SettingsDep, with_secrets: bool = False, scope: str = "shared"
) -> str:
    """The connections another orchestrator should have (``scope: shared``) as a file body."""
    try:
        return service.export_shared(settings, with_secrets=with_secrets, scope=scope)
    except service.Refused as exc:
        raise _refuse(exc) from exc


@api.post("/import")
async def import_shared(
    body: ImportIn,
    settings: SettingsDep,
    adapters: AdaptersDep,
    session: SessionDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    try:
        added, skipped = service.import_shared(settings, body.body)
    except service.Refused as exc:
        raise _refuse(exc) from exc
    _audit(session, admin, "connection.import_shared", "connections", added=added, skipped=skipped)
    await _apply(settings, adapters)
    return _view(settings) | {"added": added, "skipped": skipped}
