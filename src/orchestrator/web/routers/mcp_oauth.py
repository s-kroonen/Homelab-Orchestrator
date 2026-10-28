"""The OAuth authorisation server behind the MCP connector (docs/mcp.md).

What Claude's connectors expect (claude.com/docs/connectors/building/authentication): metadata
at the well-known paths, open client registration (RFC 7591) or a client metadata document,
authorisation code + PKCE (S256) only, a consent page, a form-encoded token endpoint with
rotating refresh tokens. The consent page is where a logged-in admin chooses the role and scopes
the connection gets - the OAuth part only proves the admin said yes.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlmodel import Session

from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import McpClient
from orchestrator.mcp import cimd, store
from orchestrator.mcp.scopes import (
    OFFLINE_ACCESS,
    ROLE_BLURBS,
    ROLES,
    SCOPES,
    parse_scope,
    role_of,
    scopes_for_role,
)
from orchestrator.mcp.store import OAuthError
from orchestrator.web.auth import get_current_admin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.templating import templates

router = APIRouter(tags=["mcp-oauth"])
_templates = templates

#: Where Claude's own connectors call back from (both spellings; the host is shown to the admin).
KNOWN_CALLBACK_HOSTS = {"claude.ai", "claude.com"}
_NONCE = "mcp_consent_nonce"


def _enabled(settings: Settings) -> None:
    if not settings.mcp_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP is switched off")


def _json(body: dict[str, Any], code: int = 200, **headers: str) -> JSONResponse:
    return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store", **headers})


def _error(exc: OAuthError) -> JSONResponse:
    body = {"error": exc.error}
    if exc.description:
        body["error_description"] = exc.description
    headers = {"WWW-Authenticate": 'Basic realm="oauth"'} if exc.status == 401 else {}
    return _json(body, exc.status, **headers)


# -- metadata ----------------------------------------------------------------------------------


def resource_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "resource": store.mcp_url(settings),
        "authorization_servers": [store.origin(settings)],
        "scopes_supported": [*SCOPES, OFFLINE_ACCESS],
        "bearer_methods_supported": ["header"],
        "resource_name": "Homelab Orchestrator",
    }


def server_metadata(settings: Settings) -> dict[str, Any]:
    base = store.origin(settings)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [*SCOPES, OFFLINE_ACCESS],
        "client_id_metadata_document_supported": True,
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(settings: SettingsDep) -> JSONResponse:
    _enabled(settings)
    return _json(resource_metadata(settings))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/{rest:path}")
async def authorization_server(settings: SettingsDep, rest: str = "") -> JSONResponse:
    _enabled(settings)
    return _json(server_metadata(settings))


# -- registration ------------------------------------------------------------------------------


@router.post("/oauth/register")
async def register(request: Request, session: SessionDep, settings: SettingsDep) -> JSONResponse:
    _enabled(settings)
    try:
        body = await request.json()
    except ValueError:
        return _error(OAuthError("invalid_client_metadata", "body must be JSON"))
    if not isinstance(body, dict):
        return _error(OAuthError("invalid_client_metadata", "body must be a JSON object"))
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        return _error(OAuthError("invalid_redirect_uri", "redirect_uris must be a list of strings"))
    try:
        client = store.register_client(
            session, name=str(body.get("client_name") or ""), redirect_uris=uris
        )
        session.commit()
    except OAuthError as exc:
        return _error(exc)
    return _json(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(store.aware(client.created_at).timestamp()),  # type: ignore[union-attr]
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        201,
    )


# -- authorize ---------------------------------------------------------------------------------


async def _client_for(session: Session, settings: Settings, client_id: str) -> McpClient:
    """The registered client, or (for an https client_id) the one its metadata document
    describes. Raises OAuthError when there is none."""
    if cimd.is_cimd_id(client_id):
        known = store.get_client(session, client_id)
        fresh = known is not None and (
            (store.now() - (store.aware(known.created_at) or store.now())).total_seconds() < 86400
        )
        if known is not None and fresh:
            return known
        doc = await cimd.fetch_client_metadata(settings, client_id)
        client = store.upsert_cimd_client(
            session,
            client_id=client_id,
            name=str(doc.get("client_name") or client_id),
            redirect_uris=list(doc["redirect_uris"]),
        )
        session.commit()
        return client
    registered = store.get_client(session, client_id)
    if registered is None:
        raise OAuthError("invalid_client", "unknown client_id", status=401)
    return registered


def _page_error(request: Request, message: str) -> HTMLResponse:
    return _templates.TemplateResponse(
        request, "mcp_consent.html", {"admin": None, "problem": message}, status_code=400
    )


def _back(redirect_uri: str, state: str, **params: str) -> RedirectResponse:
    query = {k: v for k, v in {**params, "state": state}.items() if v}
    sep = "&" if urlsplit(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(query)}", status_code=303)


def _describe_redirect(uri: str) -> dict[str, Any]:
    parts = urlsplit(uri)
    host = parts.netloc or parts.scheme + "://"
    return {
        "uri": uri,
        "host": host,
        "known": (parts.hostname or "") in KNOWN_CALLBACK_HOSTS,
        "loopback": store.is_loopback(uri),
    }


@router.get("/oauth/authorize", response_model=None)
async def authorize(request: Request, session: SessionDep, settings: SettingsDep) -> Response:
    _enabled(settings)
    q = request.query_params
    client_id, redirect_uri = q.get("client_id", ""), q.get("redirect_uri", "")
    state = q.get("state", "")
    try:
        client = await _client_for(session, settings, client_id)
    except OAuthError as exc:
        return _page_error(request, f"{exc.error}: {exc.description}")
    if not redirect_uri and len(client.redirect_uris) == 1:
        redirect_uri = client.redirect_uris[0]
    if not store.redirect_matches(client.redirect_uris, redirect_uri):
        return _page_error(request, "redirect_uri is not one this client registered")
    if q.get("response_type") != "code":
        return _back(redirect_uri, state, error="unsupported_response_type")
    if not q.get("code_challenge") or q.get("code_challenge_method") != "S256":
        return _back(
            redirect_uri,
            state,
            error="invalid_request",
            error_description="PKCE with code_challenge_method=S256 is required",
        )
    resource = q.get("resource", "")
    if resource and resource.rstrip("/") != store.mcp_url(settings):
        return _back(redirect_uri, state, error="invalid_target")

    admin = get_current_admin(request, session)
    if admin is None:
        target = request.url.path + ("?" + request.url.query if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)

    requested = parse_scope(q.get("scope", ""))
    nonce = secrets.token_urlsafe(16)
    request.session[_NONCE] = nonce
    return _templates.TemplateResponse(
        request,
        "mcp_consent.html",
        {
            "admin": admin,
            "problem": None,
            "client": client,
            "redirect": _describe_redirect(redirect_uri),
            "requested": requested,
            "scopes": SCOPES,
            "roles": {r: {"scopes": list(s), "blurb": ROLE_BLURBS[r]} for r, s in ROLES.items()},
            "role": "viewer",
            "selected": scopes_for_role("viewer"),
            "nonce": nonce,
            "params": {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": q.get("code_challenge", ""),
                "resource": resource,
                "scope": q.get("scope", ""),
            },
        },
    )


@router.post("/oauth/authorize", response_model=None)
async def authorize_decision(
    request: Request, session: SessionDep, settings: SettingsDep
) -> Response:
    _enabled(settings)
    admin = get_current_admin(request, session)
    if admin is None:
        return _page_error(request, "your login expired - start the connection again")
    form = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
    scopes_chosen = parse_qs((await request.body()).decode()).get("scopes", [])
    expected = request.session.pop(_NONCE, None)
    if not expected or not secrets.compare_digest(expected, form.get("nonce", "")):
        return _page_error(request, "this consent form is stale - start the connection again")
    try:
        client = await _client_for(session, settings, form.get("client_id", ""))
    except OAuthError as exc:
        return _page_error(request, f"{exc.error}: {exc.description}")
    redirect_uri, state = form.get("redirect_uri", ""), form.get("state", "")
    if not store.redirect_matches(client.redirect_uris, redirect_uri):
        return _page_error(request, "redirect_uri is not one this client registered")
    if not form.get("code_challenge"):
        return _page_error(request, "the authorization request had no PKCE challenge")

    if form.get("decision") != "approve":
        return _back(redirect_uri, state, error="access_denied")
    scopes = parse_scope(scopes_chosen)
    if not scopes:
        return _page_error(request, "choose at least one permission")
    label = (form.get("label") or client.name or "connector").strip()[:80]
    code = store.issue_code(
        session,
        client_id=client.client_id,
        redirect_uri=redirect_uri,
        code_challenge=form["code_challenge"],
        scopes=scopes,
        role=role_of(scopes),
        label=label,
        confirm_writes=form.get("confirm_writes") == "on",
        admin_username=admin.username,
        resource=form.get("resource", ""),
    )
    audit.record(
        session,
        actor=admin.username,
        action="mcp.allowed",
        target=f"mcp:{label}",
        details={"client": client.name, "role": role_of(scopes), "scopes": scopes},
    )
    session.commit()
    return _back(redirect_uri, state, code=code)


# -- token -------------------------------------------------------------------------------------


def _basic_client_id(request: Request) -> str:
    import base64

    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            return base64.b64decode(header[6:]).decode().split(":", 1)[0]
        except (ValueError, UnicodeDecodeError):
            return ""
    return ""


@router.post("/oauth/token")
async def token(request: Request, session: SessionDep, settings: SettingsDep) -> JSONResponse:
    _enabled(settings)
    body = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
    client_id = body.get("client_id") or _basic_client_id(request)
    grant_type = body.get("grant_type", "")
    try:
        if grant_type == "authorization_code":
            if store.get_client(session, client_id) is None:
                raise OAuthError("invalid_client", "unknown client_id", status=401)
            tokens = store.exchange_code(
                session,
                settings,
                code=body.get("code", ""),
                client_id=client_id,
                redirect_uri=body.get("redirect_uri", ""),
                verifier=body.get("code_verifier", ""),
                resource=body.get("resource", "").rstrip("/"),
            )
        elif grant_type == "refresh_token":
            if client_id and store.get_client(session, client_id) is None:
                raise OAuthError("invalid_client", "unknown client_id", status=401)
            tokens = store.refresh_tokens(
                session, settings, refresh_token=body.get("refresh_token", ""), client_id=client_id
            )
        else:
            raise OAuthError("unsupported_grant_type", f"{grant_type or 'no grant_type'}")
    except OAuthError as exc:
        return _error(exc)
    client = store.get_client(session, client_id)
    if client is not None:
        client.last_seen_at = store.now()
        session.add(client)
        session.commit()
    return _json(tokens.as_dict(), Pragma="no-cache")


@router.post("/oauth/revoke")
async def revoke(request: Request, session: SessionDep, settings: SettingsDep) -> JSONResponse:
    _enabled(settings)
    body = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
    if body.get("token"):
        store.revoke_token(session, body["token"])
    return _json({})  # RFC 7009: 200 whether or not the token was known
