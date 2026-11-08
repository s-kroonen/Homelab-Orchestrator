"""Passkey login/registration + admin-invite links.

Stateless across the begin/complete round trip except for the WebAuthn
challenge itself (held in the signed session cookie, see ``web/auth.py``):
the client resends ``username``/``device_label``/``invite_token`` at
``/complete`` rather than the server stashing them in between, so a stale or
abandoned ceremony can't leave half-applied session state.

Registration always needs a valid, unused, unexpired invite link (OR-58), also while no
admin exists: the first admin registers with the setup link the orchestrator prints in its log
(and ``orchestrator-cli admin reset`` / ``admin invite`` print on the host). See
``web/invites.py``. An invite made for an existing account adds a passkey to it instead of
creating an admin.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, desc, select

from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import Admin, PendingApproval, WebAuthnCredential
from orchestrator.domain.enums import ApprovalKind, ApprovalStatus, AuditResult
from orchestrator.web import auth as auth_lib
from orchestrator.web import invites as invites_lib
from orchestrator.web.auth import CurrentAdmin
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.templating import templates

router = APIRouter(tags=["auth"])
_templates = templates


def _admin_count(session: Session) -> int:
    return invites_lib.admin_count(session)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request, session: SessionDep, invite: str | None = None) -> Any:
    invite_row = invites_lib.find_open_invite(session, invite) if invite else None
    target = invites_lib.invite_target(session, invite_row) if invite_row else None
    return _templates.TemplateResponse(
        request,
        "login.html",
        {
            "bootstrap_open": _admin_count(session) == 0,
            "invite_token": invite if invite_row else None,
            "invite_for": target.username if target else None,
            "invite_invalid": bool(invite) and invite_row is None,
            "can_register": invite_row is not None,
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
    auth_lib.login_admin(request, admin, stored)
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
    invite = _check_registration_allowed(session, body.username, body.invite_token)
    target = invites_lib.invite_target(session, invite)
    if target is not None:
        existing_ids = list(
            session.exec(
                select(WebAuthnCredential.credential_id).where(
                    WebAuthnCredential.admin_id == target.id
                )
            ).all()
        )
        options = auth_lib.build_registration_options(
            settings,
            user_handle=f"admin:{target.id}".encode(),
            username=target.username,
            display_name=target.display_name or target.username,
            exclude_credential_ids=existing_ids,
        )
    else:
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

    target = invites_lib.invite_target(session, invite)
    admin = target or Admin(username=body.username, display_name=body.username)
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
        label=body.device_label or ("device" if target else "default"),
    )
    session.add(cred)

    invite.status = ApprovalStatus.CONSUMED
    invite.approved_by = admin.username
    invite.approved_at = auth_lib.utcnow()
    session.add(invite)

    audit.record(
        session,
        actor=admin.username,
        action="auth.credential.added" if target else "auth.register",
        target=f"admin:{admin.id}",
        details={"via_invite": True, "setup": bool((invite.payload or {}).get("setup"))},
    )
    session.commit()
    session.refresh(cred)

    auth_lib.login_admin(request, admin, cred)
    return {"status": "ok", "username": admin.username}


def _check_registration_allowed(
    session: Session, username: str, invite_token: str | None
) -> PendingApproval:
    """The open invite this registration uses; 403 without one, also while no admin exists."""
    invite = invites_lib.find_open_invite(session, invite_token or "")
    if invite is None:
        if not invite_token and _admin_count(session) == 0:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "the first admin registers with the setup link: it is in the orchestrator's log, "
                "or run `orchestrator-cli admin invite` on the host",
            )
        if not invite_token:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "an invite link from an existing admin is required"
            )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invite link is invalid, used, or expired")
    target = invites_lib.invite_target(session, invite)
    if target is not None:
        if target.disabled_at is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "that account is disabled")
        return invite  # a passkey for an existing account: its name, not the one typed
    existing = session.exec(select(Admin).where(Admin.username == username)).one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already registered")
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
    session.refresh(cred)
    auth_lib.remember_device(request, cred)  # it was just made on this device
    return {"status": "ok"}


def credentials_view(
    session: Session, admin: Admin, this_device: int | None
) -> list[dict[str, Any]]:
    """The signed-in admin's passkeys, newest first; ``this_device`` marks the one this browser
    signed in (or was added) with."""
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
            "this_device": this_device is not None and c.id == this_device,
        }
        for c in creds
    ]


@router.get("/api/auth/credentials")
async def list_credentials(
    admin: CurrentAdmin, request: Request, session: SessionDep
) -> list[dict[str, Any]]:
    return credentials_view(session, admin, auth_lib.current_device(request))


class CredentialPatch(BaseModel):
    label: str


def _own_credential(session: Session, admin: Admin, credential_id: int) -> WebAuthnCredential:
    cred = session.get(WebAuthnCredential, credential_id)
    if cred is None or cred.admin_id != admin.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such passkey")
    return cred


@router.patch("/api/auth/credentials/{credential_id}")
async def rename_credential(
    credential_id: int, body: CredentialPatch, admin: CurrentAdmin, session: SessionDep
) -> dict[str, Any]:
    cred = _own_credential(session, admin, credential_id)
    label = body.label.strip()[:60]
    if not label:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "give the passkey a name")
    old, cred.label = cred.label, label
    session.add(cred)
    audit.record(
        session,
        actor=admin.username,
        action="auth.credential.renamed",
        target=f"admin:{admin.id}",
        details={"credential": cred.id, "from": old, "to": label},
    )
    session.commit()
    return {"status": "ok", "label": label}


@router.delete("/api/auth/credentials/{credential_id}")
async def remove_credential(
    credential_id: int, admin: CurrentAdmin, request: Request, session: SessionDep
) -> dict[str, Any]:
    """Remove one of your passkeys. Every session it signed in ends with it (web/auth.py). Your
    last one is refused: without a passkey nobody can sign in as you again."""
    cred = _own_credential(session, admin, credential_id)
    left = session.exec(
        select(WebAuthnCredential).where(WebAuthnCredential.admin_id == admin.id)
    ).all()
    if len(left) <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this is your only passkey: add another device first, or you could not sign in again",
        )
    this_device = auth_lib.current_device(request) == cred.id
    label = cred.label
    session.delete(cred)
    audit.record(
        session,
        actor=admin.username,
        action="auth.credential.removed",
        target=f"admin:{admin.id}",
        details={"credential": credential_id, "label": label},
    )
    session.commit()
    if this_device:
        auth_lib.logout_admin(request)
    return {"status": "ok", "signed_out": this_device}


# ---------------------------------------------------------------------------
# Admins
# ---------------------------------------------------------------------------


def admins_view(session: Session, me: Admin) -> list[dict[str, Any]]:
    """Every admin, oldest first, with how many passkeys they have and when one was last used."""
    creds = session.exec(select(WebAuthnCredential)).all()
    out = []
    for a in session.exec(select(Admin).order_by(Admin.created_at)).all():  # type: ignore[arg-type]
        mine = [c for c in creds if c.admin_id == a.id]
        used = [c.last_used_at for c in mine if c.last_used_at]
        out.append(
            {
                "id": a.id,
                "username": a.username,
                "created_at": a.created_at.isoformat(),
                "disabled_at": a.disabled_at.isoformat() if a.disabled_at else None,
                "passkeys": len(mine),
                "last_used_at": max(used).isoformat() if used else None,
                "you": a.id == me.id,
            }
        )
    return out


@router.get("/api/auth/admins")
async def list_admins(admin: CurrentAdmin, session: SessionDep) -> list[dict[str, Any]]:
    return admins_view(session, admin)


def _other_admin(session: Session, me: Admin, admin_id: int, verb: str) -> Admin:
    """The admin to act on: never yourself. That also keeps one active admin at all times: you
    are active (you are signed in), and only another admin can disable or remove you."""
    target = session.get(Admin, admin_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such admin")
    if target.id == me.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"you cannot {verb} yourself: another admin has to"
        )
    return target


@router.post("/api/auth/admins/{admin_id}/disable")
async def disable_admin(admin_id: int, admin: CurrentAdmin, session: SessionDep) -> dict[str, Any]:
    """They can no longer sign in, and every session they have ends now (also if they are
    enabled again later: they sign in afresh)."""
    target = _other_admin(session, admin, admin_id, "disable")
    if target.disabled_at is None:
        target.disabled_at = auth_lib.utcnow()
        target.session_version += 1
        session.add(target)
        audit.record(
            session, actor=admin.username, action="auth.admin.disabled", target=f"admin:{target.id}"
        )
        session.commit()
    return {"status": "ok"}


@router.post("/api/auth/admins/{admin_id}/enable")
async def enable_admin(admin_id: int, admin: CurrentAdmin, session: SessionDep) -> dict[str, Any]:
    target = _other_admin(session, admin, admin_id, "enable")
    if target.disabled_at is not None:
        target.disabled_at = None
        session.add(target)
        audit.record(
            session, actor=admin.username, action="auth.admin.enabled", target=f"admin:{target.id}"
        )
        session.commit()
    return {"status": "ok"}


@router.delete("/api/auth/admins/{admin_id}")
async def remove_admin(admin_id: int, admin: CurrentAdmin, session: SessionDep) -> dict[str, Any]:
    """Remove another admin and their passkeys. Their sessions end with the passkeys."""
    target = _other_admin(session, admin, admin_id, "remove")
    name = target.username
    for cred in session.exec(
        select(WebAuthnCredential).where(WebAuthnCredential.admin_id == target.id)
    ).all():
        session.delete(cred)
    session.flush()
    session.delete(target)
    audit.record(
        session,
        actor=admin.username,
        action="auth.admin.removed",
        target=f"admin:{admin_id}",
        details={"username": name},
    )
    session.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


class CreateInviteRequest(BaseModel):
    label: str = ""


@router.post("/api/auth/invites")
async def create_invite(
    body: CreateInviteRequest, admin: CurrentAdmin, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    invite = invites_lib.create_invite(
        session,
        requested_by=admin.username,
        ttl=timedelta(hours=settings.invite_ttl_hours),
        label=body.label,
    )
    token = invite.token
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
        "url": invites_lib.invite_url(settings, token),
        "expires_at": invite.expires_at.isoformat(),
    }


def invites_view(session: Session, settings: Settings) -> list[dict[str, Any]]:
    """The newest 50 invites. An open one (pending, not expired) carries its link again, so an
    admin can copy it later; only admins see this list, and any admin can make a new link anyway."""
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
        if effective_status is ApprovalStatus.PENDING and (
            inv.expires_at < now or (inv.payload or {}).get("setup")
        ):
            # Only signed-in admins read this, so an admin exists: a setup link is dead.
            effective_status = ApprovalStatus.EXPIRED
        is_open = effective_status is ApprovalStatus.PENDING
        out.append(
            {
                "id": inv.id,
                "label": inv.payload.get("label", ""),
                "requested_by": inv.requested_by,
                "requested_at": inv.requested_at.isoformat(),
                "expires_at": inv.expires_at.isoformat(),
                "status": effective_status.value,
                "approved_by": inv.approved_by,
                "approved_at": inv.approved_at.isoformat() if inv.approved_at else None,
                "url": invites_lib.invite_url(settings, inv.token) if is_open else None,
            }
        )
    return out


@router.get("/api/auth/invites")
async def list_invites(
    admin: CurrentAdmin, session: SessionDep, settings: SettingsDep
) -> list[dict[str, Any]]:
    return invites_view(session, settings)


@router.delete("/api/auth/invites/{invite_id}")
async def revoke_invite(invite_id: int, admin: CurrentAdmin, session: SessionDep) -> dict[str, Any]:
    """An open invite stops working now; a used or expired one is left as it is."""
    invite = session.get(PendingApproval, invite_id)
    if invite is None or invite.kind is not ApprovalKind.ADMIN_INVITE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such invite")
    if invite.status is ApprovalStatus.PENDING:
        invite.status = ApprovalStatus.DENIED
        invite.approved_by = admin.username
        invite.approved_at = auth_lib.utcnow()
        session.add(invite)
        audit.record(
            session,
            actor=admin.username,
            action="auth.invite.revoked",
            details={"invite": invite.id, "label": (invite.payload or {}).get("label", "")},
        )
        session.commit()
    return {"status": "ok"}
