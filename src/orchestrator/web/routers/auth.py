"""Passkey login/registration + admin-invite links.

Stateless across the begin/complete round trip except for the WebAuthn
challenge itself (held in the signed session cookie, see ``web/auth.py``):
the client resends ``username``/``device_label``/``invite_token`` at
``/complete`` rather than the server stashing them in between, so a stale or
abandoned ceremony can't leave half-applied session state.

Bootstrap rule: registration needs no invite while the ``admin`` table is
empty. Once at least one admin exists, every new registration needs a valid,
unused, unexpired invite link — created by an already-logged-in admin via
``POST /api/auth/invites`` and consumed here.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import desc, func, select

from orchestrator.audit import log as audit
from orchestrator.db.models import Admin, PendingApproval, WebAuthnCredential
from orchestrator.domain.enums import ApprovalKind, ApprovalStatus, AuditResult
from orchestrator.web import auth as auth_lib
from orchestrator.web.auth import CurrentAdmin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.templating import templates

router = APIRouter(tags=["auth"])
_templates = templates


def _admin_count(session: SessionDep) -> int:
    return session.exec(select(func.count()).select_from(Admin)).one()


def _find_invite(session: SessionDep, token: str) -> PendingApproval | None:
    invite = session.exec(
        select(PendingApproval).where(
            PendingApproval.token == token,
            PendingApproval.kind == ApprovalKind.ADMIN_INVITE,
        )
    ).one_or_none()
    if invite is None:
        return None
    if invite.status is not ApprovalStatus.PENDING:
        return None
    if invite.expires_at < auth_lib.utcnow():
        return None
    return invite


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request, session: SessionDep, invite: str | None = None) -> Any:
    bootstrap_open = _admin_count(session) == 0
    invite_row = _find_invite(session, invite) if invite else None
    return _templates.TemplateResponse(
        request,
        "login.html",
        {
            "bootstrap_open": bootstrap_open,
            "invite_token": invite if invite_row else None,
            "invite_invalid": bool(invite) and invite_row is None,
            "can_register": bootstrap_open or invite_row is not None,
        },
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class LoginBeginRequest(BaseModel):
    username: str


@router.post("/api/auth/login/begin")
async def login_begin(
    body: LoginBeginRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    admin = session.exec(select(Admin).where(Admin.username == body.username)).one_or_none()
    credential_ids: list[bytes] = []
    if admin is not None and admin.disabled_at is None:
        credential_ids = list(
            session.exec(
                select(WebAuthnCredential.credential_id).where(
                    WebAuthnCredential.admin_id == admin.id
                )
            ).all()
        )
    options = auth_lib.build_authentication_options(settings, allow_credential_ids=credential_ids)
    auth_lib.store_challenge(request, auth_lib.base64url_to_bytes(options["challenge"]))
    return options


class LoginCompleteRequest(BaseModel):
    credential: dict[str, Any]


@router.post("/api/auth/login/complete")
async def login_complete(
    body: LoginCompleteRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    challenge = auth_lib.pop_challenge(request)
    raw_id = body.credential.get("rawId") or body.credential.get("id")
    stored = auth_lib.find_credential_by_raw_id(session, raw_id) if raw_id else None
    if stored is None:
        audit.record(session, actor="unknown", action="auth.login", result=AuditResult.DENIED)
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown credential")

    admin = session.get(Admin, stored.admin_id)
    if admin is None or admin.disabled_at is not None:
        audit.record(
            session,
            actor=admin.username if admin else "unknown",
            action="auth.login",
            result=AuditResult.DENIED,
        )
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account disabled")

    try:
        verified = auth_lib.verify_authentication(
            settings,
            credential=body.credential,
            expected_challenge=challenge,
            stored_credential=stored,
        )
    except Exception as exc:
        audit.record(
            session,
            actor=admin.username,
            action="auth.login",
            result=AuditResult.DENIED,
            details={"error": str(exc)},
        )
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "verification failed") from exc

    stored.sign_count = verified.new_sign_count
    stored.last_used_at = auth_lib.utcnow()
    session.add(stored)
    audit.record(session, actor=admin.username, action="auth.login", target=f"admin:{admin.id}")
    session.commit()

    assert admin.id is not None  # loaded from the DB by a non-null FK
    auth_lib.login_admin(request, admin.id)
    return {"status": "ok", "username": admin.username}


@router.post("/api/auth/logout")
async def logout(request: Request, session: SessionDep) -> dict[str, Any]:
    admin = auth_lib.get_current_admin(request, session)
    if admin is not None:
        audit.record(session, actor=admin.username, action="auth.logout")
        session.commit()
    auth_lib.logout_admin(request)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Registration (new admin — bootstrap or invite)
# ---------------------------------------------------------------------------


class RegisterBeginRequest(BaseModel):
    username: str
    invite_token: str | None = None


@router.post("/api/auth/register/begin")
async def register_begin(
    body: RegisterBeginRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    _check_registration_allowed(session, body.username, body.invite_token)
    options = auth_lib.build_registration_options(
        settings,
        user_handle=auth_lib.new_user_handle(),
        username=body.username,
        display_name=body.username,
    )
    auth_lib.store_challenge(request, auth_lib.base64url_to_bytes(options["challenge"]))
    return options


class RegisterCompleteRequest(BaseModel):
    username: str
    device_label: str = ""
    invite_token: str | None = None
    credential: dict[str, Any]


@router.post("/api/auth/register/complete")
async def register_complete(
    body: RegisterCompleteRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    invite = _check_registration_allowed(session, body.username, body.invite_token)
    challenge = auth_lib.pop_challenge(request)

    try:
        verified = auth_lib.verify_registration(
            settings, credential=body.credential, expected_challenge=challenge
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"verification failed: {exc}") from exc

    admin = Admin(username=body.username, display_name=body.username)
    session.add(admin)
    session.flush()
    assert admin.id is not None  # populated by the flush above

    transports = body.credential.get("response", {}).get("transports") or []
    cred = WebAuthnCredential(
        admin_id=admin.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=list(transports),
        aaguid=str(verified.aaguid or ""),
        label=body.device_label or "default",
    )
    session.add(cred)

    if invite is not None:
        invite.status = ApprovalStatus.CONSUMED
        invite.approved_by = body.username
        invite.approved_at = auth_lib.utcnow()
        session.add(invite)

    audit.record(
        session,
        actor=body.username,
        action="auth.register",
        target=f"admin:{admin.id}",
        details={"via_invite": invite is not None},
    )
    session.commit()

    auth_lib.login_admin(request, admin.id)
    return {"status": "ok", "username": admin.username}


def _check_registration_allowed(
    session: SessionDep, username: str, invite_token: str | None
) -> PendingApproval | None:
    existing = session.exec(select(Admin).where(Admin.username == username)).one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already registered")

    if _admin_count(session) == 0:
        return None  # bootstrap: first admin needs no invite

    if not invite_token:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "an invite link from an existing admin is required"
        )
    invite = _find_invite(session, invite_token)
    if invite is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invite link is invalid, used, or expired")
    return invite


# ---------------------------------------------------------------------------
# Adding a device to your OWN account (already logged in)
# ---------------------------------------------------------------------------


@router.post("/api/auth/credentials/add/begin")
async def add_credential_begin(
    admin: CurrentAdmin, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    existing_ids = list(
        session.exec(
            select(WebAuthnCredential.credential_id).where(WebAuthnCredential.admin_id == admin.id)
        ).all()
    )
    options = auth_lib.build_registration_options(
        settings,
        user_handle=f"admin:{admin.id}".encode(),
        username=admin.username,
        display_name=admin.display_name or admin.username,
        exclude_credential_ids=existing_ids,
    )
    auth_lib.store_challenge(request, auth_lib.base64url_to_bytes(options["challenge"]))
    return options


class AddCredentialCompleteRequest(BaseModel):
    device_label: str = ""
    credential: dict[str, Any]


@router.post("/api/auth/credentials/add/complete")
async def add_credential_complete(
    body: AddCredentialCompleteRequest,
    admin: CurrentAdmin,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    challenge = auth_lib.pop_challenge(request)
    try:
        verified = auth_lib.verify_registration(
            settings, credential=body.credential, expected_challenge=challenge
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"verification failed: {exc}") from exc

    assert admin.id is not None  # loaded from the DB by require_admin
    transports = body.credential.get("response", {}).get("transports") or []
    cred = WebAuthnCredential(
        admin_id=admin.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=list(transports),
        aaguid=str(verified.aaguid or ""),
        label=body.device_label or "device",
    )
    session.add(cred)
    audit.record(
        session, actor=admin.username, action="auth.credential.added", target=f"admin:{admin.id}"
    )
    session.commit()
    return {"status": "ok"}


@router.get("/api/auth/credentials")
async def list_credentials(admin: CurrentAdmin, session: SessionDep) -> list[dict[str, Any]]:
    creds = session.exec(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.admin_id == admin.id)
        .order_by(desc(WebAuthnCredential.created_at))
    ).all()
    return [
        {
            "id": c.id,
            "label": c.label,
            "created_at": c.created_at.isoformat(),
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        }
        for c in creds
    ]


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


class CreateInviteRequest(BaseModel):
    label: str = ""


@router.post("/api/auth/invites")
async def create_invite(
    body: CreateInviteRequest, admin: CurrentAdmin, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    invite = PendingApproval(
        kind=ApprovalKind.ADMIN_INVITE,
        token=token,
        payload={"label": body.label},
        requested_by=admin.username,
        expires_at=auth_lib.utcnow() + timedelta(hours=settings.invite_ttl_hours),
    )
    session.add(invite)
    audit.record(
        session,
        actor=admin.username,
        action="auth.invite.created",
        details={"label": body.label, "expires_at": invite.expires_at.isoformat()},
    )
    session.commit()
    session.refresh(invite)
    return {
        "token": token,
        "url": f"{settings.webauthn_origin}/login?invite={token}",
        "expires_at": invite.expires_at.isoformat(),
    }


@router.get("/api/auth/invites")
async def list_invites(admin: CurrentAdmin, session: SessionDep) -> list[dict[str, Any]]:
    invites = session.exec(
        select(PendingApproval)
        .where(PendingApproval.kind == ApprovalKind.ADMIN_INVITE)
        .order_by(desc(PendingApproval.requested_at))
        .limit(50)
    ).all()
    now = auth_lib.utcnow()
    out = []
    for inv in invites:
        effective_status = inv.status
        if effective_status is ApprovalStatus.PENDING and inv.expires_at < now:
            effective_status = ApprovalStatus.EXPIRED
        out.append(
            {
                "label": inv.payload.get("label", ""),
                "requested_by": inv.requested_by,
                "requested_at": inv.requested_at.isoformat(),
                "expires_at": inv.expires_at.isoformat(),
                "status": effective_status.value,
                "approved_by": inv.approved_by,
            }
        )
    return out
