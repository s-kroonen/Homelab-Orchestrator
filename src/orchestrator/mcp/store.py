"""OAuth clients, authorisation codes, tokens and the call history.

Tokens are random and shown once; only their SHA-256 is stored. A refresh token is rotated on
every use, and presenting an already-rotated one revokes the whole connection (a copy leaked).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, col, select

from orchestrator.config import Settings
from orchestrator.db.models import McpAuthCode, McpCall, McpClient, McpGrant
from orchestrator.mcp.scopes import parse_scope, role_of

CODE_TTL = timedelta(minutes=10)
MAX_CLIENTS = 200
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "blob", "about", "ftp"}
_SECRETISH = re.compile(r"pass|secret|token|key|auth|credential", re.I)


class OAuthError(Exception):
    """An OAuth error response (RFC 6749 section 5.2)."""

    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status


def now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def origin(settings: Settings) -> str:
    """The address assistants reach this server on (no trailing slash)."""
    return (settings.external_url or settings.webauthn_origin).strip().rstrip("/")


def mcp_url(settings: Settings) -> str:
    return f"{origin(settings)}/mcp"


# -- redirect URIs -----------------------------------------------------------------------------


def redirect_uri_problem(uri: str) -> str | None:
    """Why ``uri`` cannot be a redirect URI, or None. https anywhere, http only on loopback,
    or an app's own scheme (cursor://, vscode://); never a scheme that runs code."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "not a URL"
    scheme = parts.scheme.lower()
    if not scheme or parts.fragment:
        return "must be an absolute URI without a fragment"
    if scheme in _BAD_SCHEMES:
        return f"the {scheme}: scheme is not allowed"
    if scheme == "http":
        if (parts.hostname or "") not in {h.strip("[]") for h in _LOOPBACK}:
            return "http is only allowed for localhost / 127.0.0.1"
    elif scheme == "https" and not parts.hostname:
        return "no host"
    return None


def is_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in {h.strip("[]") for h in _LOOPBACK}


def redirect_matches(registered: list[str], candidate: str) -> bool:
    """Exact match - except a loopback redirect may use any port (RFC 8252), and
    ``localhost`` and ``127.0.0.1`` are the same machine."""
    if candidate in registered:
        return True
    if not is_loopback(candidate):
        return False
    c = urlsplit(candidate)
    for reg in registered:
        if not is_loopback(reg):
            continue
        r = urlsplit(reg)
        if r.path == c.path and r.query == c.query:
            return True
    return False


# -- clients -----------------------------------------------------------------------------------


def register_client(
    session: Session, *, name: str, redirect_uris: list[str], kind: str = "dcr"
) -> McpClient:
    if not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "redirect_uris is required")
    for uri in redirect_uris:
        problem = redirect_uri_problem(uri)
        if problem:
            raise OAuthError("invalid_redirect_uri", f"{uri}: {problem}")
    _prune_clients(session)
    if len(session.exec(select(McpClient)).all()) >= MAX_CLIENTS:
        raise OAuthError("temporarily_unavailable", "too many registered clients", status=429)
    client = McpClient(
        client_id=f"mcp_{secrets.token_urlsafe(16)}",
        name=(name or "MCP client").strip()[:80],
        redirect_uris=list(redirect_uris),
        kind=kind,
    )
    session.add(client)
    session.flush()
    return client


def _prune_clients(session: Session) -> None:
    """Registration is open, so forget clients that registered and never got anywhere."""
    cutoff = now() - timedelta(days=7)
    used = {g.client_id for g in session.exec(select(McpGrant)).all()}
    for c in session.exec(select(McpClient).where(McpClient.kind == "dcr")).all():
        created = aware(c.created_at)
        if c.client_id not in used and c.last_seen_at is None and created and created < cutoff:
            session.delete(c)
    session.flush()


def get_client(session: Session, client_id: str) -> McpClient | None:
    return session.exec(select(McpClient).where(McpClient.client_id == client_id)).one_or_none()


def upsert_cimd_client(
    session: Session, *, client_id: str, name: str, redirect_uris: list[str]
) -> McpClient:
    for uri in redirect_uris:
        problem = redirect_uri_problem(uri)
        if problem:
            raise OAuthError("invalid_client", f"client metadata redirect {uri}: {problem}")
    client = get_client(session, client_id)
    if client is None:
        client = McpClient(client_id=client_id, kind="cimd")
    client.name = (name or client_id).strip()[:80]
    client.redirect_uris = list(redirect_uris)
    client.created_at = now()  # "fetched at" for a metadata-document client
    session.add(client)
    session.flush()
    return client


# -- authorisation codes -----------------------------------------------------------------------


def issue_code(
    session: Session,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    scopes: list[str],
    role: str,
    label: str,
    confirm_writes: bool,
    admin_username: str,
    resource: str,
) -> str:
    code = new_token("mcp_code")
    session.add(
        McpAuthCode(
            code_hash=sha256(code),
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scopes=scopes,
            role=role,
            label=label,
            confirm_writes=confirm_writes,
            admin_username=admin_username,
            resource=resource,
            expires_at=now() + CODE_TTL,
        )
    )
    session.flush()
    return code


def pkce_ok(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier) <= 128:
        return False
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(expected, challenge)


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
        }


def _mint(grant: McpGrant, settings: Settings) -> Tokens:
    access, refresh = new_token("mcp_at"), new_token("mcp_rt")
    grant.access_hash = sha256(access)
    grant.access_expires_at = now() + timedelta(seconds=max(60, settings.mcp_access_ttl_s))
    grant.prev_refresh_hash = grant.refresh_hash
    grant.refresh_hash = sha256(refresh)
    grant.expires_at = now() + timedelta(days=max(1, settings.mcp_refresh_ttl_days))
    return Tokens(access, refresh, max(60, settings.mcp_access_ttl_s), " ".join(grant.scopes))


def exchange_code(
    session: Session,
    settings: Settings,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    resource: str,
) -> Tokens:
    row = session.exec(select(McpAuthCode).where(McpAuthCode.code_hash == sha256(code))).first()
    if row is None:
        raise OAuthError("invalid_grant", "unknown authorization code")
    if row.used_at is not None:
        raise OAuthError("invalid_grant", "authorization code already used")
    if (aware(row.expires_at) or now()) < now():
        raise OAuthError("invalid_grant", "authorization code expired")
    if row.client_id != client_id:
        raise OAuthError("invalid_grant", "code was issued to another client")
    if row.redirect_uri != redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
    if not pkce_ok(verifier, row.code_challenge):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    if resource and row.resource and resource != row.resource:
        raise OAuthError("invalid_target", "resource does not match the authorization request")
    row.used_at = now()
    client = get_client(session, client_id)
    grant = McpGrant(
        kind="oauth",
        label=row.label or (client.name if client else "connector"),
        client_id=client_id,
        client_name=client.name if client else "",
        admin_username=row.admin_username,
        role=row.role,
        scopes=list(row.scopes),
        confirm_writes=row.confirm_writes,
    )
    tokens = _mint(grant, settings)
    session.add(row)
    session.add(grant)
    session.commit()
    return tokens


def refresh_tokens(
    session: Session, settings: Settings, *, refresh_token: str, client_id: str
) -> Tokens:
    h = sha256(refresh_token)
    grant = session.exec(select(McpGrant).where(McpGrant.refresh_hash == h)).first()
    if grant is None:
        replayed = session.exec(select(McpGrant).where(McpGrant.prev_refresh_hash == h)).first()
        if replayed is not None and replayed.revoked_at is None:
            replayed.revoked_at = now()
            session.add(replayed)
            session.commit()
        raise OAuthError("invalid_grant", "refresh token is not valid")
    if grant.revoked_at is not None:
        raise OAuthError("invalid_grant", "this connection was revoked")
    if grant.expires_at is not None and (aware(grant.expires_at) or now()) < now():
        raise OAuthError("invalid_grant", "refresh token expired")
    if client_id and grant.client_id != client_id:
        raise OAuthError("invalid_grant", "refresh token belongs to another client")
    tokens = _mint(grant, settings)
    session.add(grant)
    session.commit()
    return tokens


def create_personal(
    session: Session,
    settings: Settings,
    *,
    label: str,
    scopes: list[str],
    admin_username: str,
    confirm_writes: bool = False,
    expires_days: int | None = None,
) -> tuple[McpGrant, str]:
    """A token made by hand for a client that cannot do OAuth. Returned once."""
    token = new_token("mcp_pt")
    grant = McpGrant(
        kind="personal",
        label=label.strip()[:80] or "personal token",
        admin_username=admin_username,
        role=role_of(scopes),
        scopes=scopes,
        confirm_writes=confirm_writes,
        access_hash=sha256(token),
        expires_at=now() + timedelta(days=expires_days) if expires_days else None,
    )
    session.add(grant)
    session.commit()
    session.refresh(grant)
    return grant, token


def authenticate(session: Session, token: str) -> McpGrant | None:
    """The live grant this access token belongs to, else None."""
    if not token:
        return None
    grant = session.exec(select(McpGrant).where(McpGrant.access_hash == sha256(token))).first()
    if grant is None or grant.revoked_at is not None:
        return None
    moment = now()
    if grant.access_expires_at is not None and (aware(grant.access_expires_at) or moment) < moment:
        return None
    if (
        grant.kind == "personal"
        and grant.expires_at is not None
        and (aware(grant.expires_at) or moment) < moment
    ):
        return None
    last = aware(grant.last_used_at)
    if last is None or moment - last > timedelta(seconds=30):
        grant.last_used_at = moment
        session.add(grant)
        session.commit()
    return grant


def revoke_token(session: Session, token: str) -> bool:
    h = sha256(token)
    grant = session.exec(
        select(McpGrant).where((McpGrant.access_hash == h) | (McpGrant.refresh_hash == h))
    ).first()
    if grant is None or grant.revoked_at is not None:
        return False
    grant.revoked_at = now()
    session.add(grant)
    session.commit()
    return True


def revoke_grant(session: Session, grant_id: int) -> McpGrant | None:
    grant = session.get(McpGrant, grant_id)
    if grant is None:
        return None
    if grant.revoked_at is None:
        grant.revoked_at = now()
        session.add(grant)
        session.commit()
    return grant


def grant_view(grant: McpGrant) -> dict[str, Any]:
    def iso(v: datetime | None) -> str | None:
        v = aware(v)
        return v.isoformat() if v else None

    moment = now()
    expired = bool(grant.expires_at and (aware(grant.expires_at) or moment) < moment)
    state = "revoked" if grant.revoked_at else "expired" if expired else "active"
    return {
        "id": grant.id,
        "kind": grant.kind,
        "label": grant.label,
        "client": grant.client_name or None,
        "admin": grant.admin_username,
        "role": grant.role,
        "scopes": list(grant.scopes),
        "confirm_writes": grant.confirm_writes,
        "state": state,
        "created_at": iso(grant.created_at),
        "last_used_at": iso(grant.last_used_at),
        "expires_at": iso(grant.expires_at),
        "revoked_at": iso(grant.revoked_at),
    }


# -- history -----------------------------------------------------------------------------------


def redact(value: Any) -> Any:
    """Arguments as the history keeps them: anything that looks like a secret is hidden."""
    if isinstance(value, dict):
        return {k: "***" if _SECRETISH.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    return value


def record_call(
    session: Session,
    grant: McpGrant,
    *,
    tool: str,
    risk: str,
    args: dict[str, Any],
    status: str,
    summary: str = "",
    duration_ms: int = 0,
    approval_token: str | None = None,
) -> McpCall:
    call = McpCall(
        grant_id=grant.id,
        label=grant.label,
        client_name=grant.client_name,
        role=grant.role,
        tool=tool,
        risk=risk,
        args=redact(args),
        status=status,
        summary=summary[:500],
        duration_ms=duration_ms,
        approval_token=approval_token,
    )
    session.add(call)
    session.commit()
    session.refresh(call)
    return call


def call_view(call: McpCall) -> dict[str, Any]:
    at = aware(call.at)
    return {
        "id": call.id,
        "at": at.isoformat() if at else None,
        "grant_id": call.grant_id,
        "label": call.label,
        "client": call.client_name or None,
        "role": call.role,
        "tool": call.tool,
        "risk": call.risk,
        "args": call.args,
        "status": call.status,
        "summary": call.summary,
        "duration_ms": call.duration_ms,
        "approval_token": call.approval_token,
        "decided_by": call.decided_by,
    }


def history(
    session: Session,
    *,
    limit: int = 100,
    grant_id: int | None = None,
    tool: str | None = None,
    status: str | None = None,
    risk: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(McpCall).order_by(col(McpCall.at).desc(), col(McpCall.id).desc())
    if grant_id is not None:
        stmt = stmt.where(McpCall.grant_id == grant_id)
    if tool:
        stmt = stmt.where(McpCall.tool == tool)
    if status:
        stmt = stmt.where(McpCall.status == status)
    if risk:
        stmt = stmt.where(McpCall.risk == risk)
    return [call_view(c) for c in session.exec(stmt.limit(max(1, min(limit, 500)))).all()]


__all__ = [
    "OAuthError",
    "Tokens",
    "authenticate",
    "create_personal",
    "exchange_code",
    "grant_view",
    "parse_scope",
    "refresh_tokens",
]
