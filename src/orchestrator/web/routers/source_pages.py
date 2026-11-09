"""Sources: one page per connection (OR-78, the Sources wireframes).

Pages:

* ``GET /sources``: every source with its status, what it has and what uses it.
* ``GET /sources/{id}?tab=...``: one source: Overview, its resources, Settings, Raw.

API (admins only):

* ``GET  /api/sources``                     every source, its last known status, what uses it
* ``POST /api/sources/check``               check every source now (Test all)
* ``GET  /api/sources/kinds``               what can be added (Add source)
* ``GET  /api/sources/{id}``                one source: its connection, status, tabs
* ``POST /api/sources/{id}/test``           check it now
* ``GET  /api/sources/{id}/overview``       tiles, needs attention, facts
* ``GET  /api/sources/{id}/resources/{key}`` one list: columns, chips, rows
* ``GET  /api/sources/{id}/raw``            its raw endpoints
* ``GET  /api/sources/{id}/raw/{key}``      one of them, as the API returns it (credentials
  redacted)

Changing a source (its settings, enable, default, remove) goes through ``/api/connections``, which
validates, encrypts, audits and applies without a restart.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from orchestrator.config import Settings
from orchestrator.connections import service as conn_service
from orchestrator.connectors import status as source_status
from orchestrator.connectors.kinds import raw_payload
from orchestrator.connectors.registry import Source, all_sources, connectors, context, get_source
from orchestrator.web.auth import get_current_admin, login_redirect, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.routers.connections import _kinds
from orchestrator.web.templating import templates

pages = APIRouter(include_in_schema=False)
api = APIRouter(prefix="/api/sources", tags=["sources"], dependencies=[Depends(require_admin)])


def _source_or_404(ident: str) -> Source:
    source = get_source(ident)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"there is no source called {ident!r}")
    return source


def _used_by(session: Session, settings: Settings, source: Source) -> list[str]:
    try:
        return conn_service.references(session, settings, source.conn)
    except Exception:
        return []


def _row(session: Session, settings: Settings, source: Source) -> dict[str, Any]:
    return {
        **source.public(),
        "status": source_status.as_dict(source.id),
        "used_by": _used_by(session, settings, source),
    }


def list_view(session: Session, settings: Settings) -> list[dict[str, Any]]:
    return [_row(session, settings, s) for s in all_sources(settings)]


def nav_groups() -> list[dict[str, Any]]:
    """The sidebar's Sources section: kinds in connector order, each with its sources and the
    worst status among them (shown on a folded kind)."""
    order = ["fail", "warn", "unknown", "ok", "off"]
    groups: dict[str, dict[str, Any]] = {}
    for source in all_sources():
        st = source_status.as_dict(source.id)["state"]
        g = groups.setdefault(
            source.connector.group, {"group": source.connector.group, "sources": [], "worst": "off"}
        )
        g["sources"].append(
            {"id": source.id, "name": source.name, "state": st, "href": source.href}
        )
        if order.index(st) < order.index(g["worst"]):
            g["worst"] = st
    return list(groups.values())


# -- pages ------------------------------------------------------------------------------------------


@pages.get("/sources", response_model=None)
async def sources_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    initial = {"sources": list_view(session, settings), "kinds": _kind_choices()}
    return templates.TemplateResponse(
        request, "sources_index.html", {"admin": admin, "initial": initial}
    )


@pages.get("/sources/{ident}", response_model=None)
async def source_page(
    ident: str, request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    admin = get_current_admin(request, session)
    if admin is None:
        return login_redirect(request)
    source = _source_or_404(ident)
    return templates.TemplateResponse(
        request,
        "source.html",
        {"admin": admin, "source": source, "initial": _detail(session, settings, source)},
    )


# -- API --------------------------------------------------------------------------------------------


def _kind_choices() -> list[dict[str, Any]]:
    """What Add source offers: every connector, with its kind's fields."""
    fields = {k["kind"]: k for k in _kinds()}
    return [
        {
            "kind": c.kind,
            "title": c.title,
            "group": c.group,
            "family": c.family,
            "blurb": c.blurb,
            "fields": fields.get(c.kind, {}).get("fields", []),
            "multi": fields.get(c.kind, {}).get("multi", False),
        }
        for c in connectors()
    ]


def _detail(session: Session, settings: Settings, source: Source) -> dict[str, Any]:
    kind = next((k for k in _kinds() if k["kind"] == source.kind), None)
    return {
        **_row(session, settings, source),
        "connection": source.conn.public(),
        "fields": kind["fields"] if kind else [],
        "multi": bool(kind and kind["multi"]),
        "tabs": [{"key": t.key, "label": t.label, "view": t.view} for t in source.connector.tabs()],
        "resources": {
            r.key: {
                "label": r.label,
                "view": r.view,
                "columns": [{"key": c.key, "label": c.label, "kind": c.kind} for c in r.columns],
                "chips": [{"key": c.key, "label": c.label} for c in r.chips],
                "actions": [
                    {"key": a.key, "label": a.label, "danger": a.danger, "bulk": a.bulk}
                    for a in r.actions
                ],
            }
            for r in source.connector.resources
        },
        "raw": [
            {"key": e.key, "label": e.label, "describe": e.describe} for e in source.connector.raw
        ],
    }


@api.get("")
async def list_sources(session: SessionDep, settings: SettingsDep) -> list[dict[str, Any]]:
    return list_view(session, settings)


@api.get("/kinds")
async def kinds() -> list[dict[str, Any]]:
    return _kind_choices()


@api.post("/check")
async def check_all(
    session: SessionDep, settings: SettingsDep, adapters: AdaptersDep
) -> list[dict[str, Any]]:
    await source_status.refresh_all(settings, adapters)
    return list_view(session, settings)


@api.get("/{ident}")
async def one_source(ident: str, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    return _detail(session, settings, _source_or_404(ident))


@api.post("/{ident}/test")
async def test_source(
    ident: str, session: SessionDep, settings: SettingsDep, adapters: AdaptersDep
) -> dict[str, Any]:
    source = _source_or_404(ident)
    await source_status.refresh(source, settings, adapters)
    return _row(session, settings, source)


def _ctx(ident: str, settings: Settings, adapters: Any) -> tuple[Source, Any]:
    source = _source_or_404(ident)
    if not source.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{source.name} is switched off")
    return source, context(source, settings, adapters)


async def _guarded(call: Any) -> Any:
    """A source's own call: its failure is the page's 502, with what went wrong."""
    try:
        return await call
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"{type(exc).__name__}: {exc}"[:400]
        ) from exc


@api.get("/{ident}/overview")
async def overview(ident: str, settings: SettingsDep, adapters: AdaptersDep) -> dict[str, Any]:
    source, ctx = _ctx(ident, settings, adapters)
    ov = await _guarded(source.connector.overview(ctx))
    return {"tiles": ov.tiles, "attention": ov.attention, "facts": ov.facts}


@api.get("/{ident}/resources/{key}")
async def resource(
    ident: str, key: str, settings: SettingsDep, adapters: AdaptersDep
) -> dict[str, Any]:
    source, ctx = _ctx(ident, settings, adapters)
    res = source.connector.resource(key)
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{source.name} has no {key!r}")
    rows = await _guarded(res.fetch(ctx))
    return {"key": res.key, "label": res.label, "rows": rows}


@api.get("/{ident}/raw")
async def raw_list(ident: str) -> list[dict[str, Any]]:
    source = _source_or_404(ident)
    return [{"key": e.key, "label": e.label, "describe": e.describe} for e in source.connector.raw]


@api.get("/{ident}/raw/{key}")
async def raw_one(
    ident: str, key: str, settings: SettingsDep, adapters: AdaptersDep
) -> dict[str, Any]:
    source, ctx = _ctx(ident, settings, adapters)
    endpoint = next((e for e in source.connector.raw if e.key == key), None)
    if endpoint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{source.name} has no raw {key!r}")
    data = await _guarded(raw_payload(endpoint, ctx))
    return {"key": key, "describe": endpoint.describe, "data": data}


__all__ = ["api", "list_view", "nav_groups", "pages"]
