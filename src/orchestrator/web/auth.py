"""Passkey (WebAuthn) authentication: ceremony helpers, session plumbing, the
``require_admin`` dependency that gates the dashboard API.

Kept separate from ``routers/auth.py`` so the WebAuthn calls can be
monkeypatched in tests the same way this repo already mocks adapters — a
real registration/authentication ceremony needs a hardware or platform
authenticator, which pytest doesn't have.

Session storage: a signed cookie (Starlette's ``SessionMiddleware``, wired in
``web/app.py`` with ``settings.session_secret``), not a DB table. It holds
the logged-in ``admin_id`` plus, briefly, the challenge for an in-flight
registration/authentication ceremony — cleared as soon as that ceremony
completes or fails.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

import webauthn
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from orchestrator.config import Settings
from orchestrator.db.models import Admin, WebAuthnCredential
from orchestrator.web.deps import SessionDep

_SESSION_ADMIN_KEY = "admin_id"
_SESSION_CHALLENGE_KEY = "webauthn_challenge"

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def login_admin(request: Request, admin_id: int) -> None:
    request.session[_SESSION_ADMIN_KEY] = admin_id
    request.session.pop(_SESSION_CHALLENGE_KEY, None)


def logout_admin(request: Request) -> None:
    request.session.clear()


def current_admin_id(request: Request) -> int | None:
    value = request.session.get(_SESSION_ADMIN_KEY)
    return int(value) if value is not None else None


def store_challenge(request: Request, challenge: bytes) -> None:
    request.session[_SESSION_CHALLENGE_KEY] = webauthn.helpers.bytes_to_base64url(challenge)


def base64url_to_bytes(value: str) -> bytes:
    """Reexported so routers don't need their own ``import webauthn``."""
    return webauthn.helpers.base64url_to_bytes(value)


def pop_challenge(request: Request) -> bytes:
    raw = request.session.pop(_SESSION_CHALLENGE_KEY, None)
    if raw is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no ceremony in progress")
    return webauthn.helpers.base64url_to_bytes(raw)


def get_current_admin(request: Request, session: Session) -> Admin | None:
    admin_id = current_admin_id(request)
    if admin_id is None:
        return None
    admin = session.get(Admin, admin_id)
    if admin is None or admin.disabled_at is not None:
        return None
    return admin


def require_admin(request: Request, session: SessionDep) -> Admin:
    """FastAPI dependency gating the JSON API. 401s rather than redirecting —
    callers are fetch()/XHR, not a browser navigation."""
    admin = get_current_admin(request, session)
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    return admin


CurrentAdmin = Annotated[Admin, Depends(require_admin)]

# ---------------------------------------------------------------------------
# WebAuthn ceremony helpers
# ---------------------------------------------------------------------------


def build_registration_options(
    settings: Settings,
    *,
    user_handle: bytes,
    username: str,
    display_name: str,
    exclude_credential_ids: list[bytes] | None = None,
) -> dict[str, Any]:
    options = webauthn.generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=user_handle,
        user_name=username,
        user_display_name=display_name or username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=cred_id) for cred_id in (exclude_credential_ids or [])
        ],
    )
    return webauthn.helpers.options_to_json_dict(options)


def verify_registration(
    settings: Settings, *, credential: dict[str, Any], expected_challenge: bytes
) -> webauthn.registration.verify_registration_response.VerifiedRegistration:
    return webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=settings.webauthn_rp_id,
        expected_origin=settings.webauthn_origin,
    )


def build_authentication_options(
    settings: Settings, *, allow_credential_ids: list[bytes]
) -> dict[str, Any]:
    options = webauthn.generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=cred_id) for cred_id in allow_credential_ids
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return webauthn.helpers.options_to_json_dict(options)


def verify_authentication(
    settings: Settings,
    *,
    credential: dict[str, Any],
    expected_challenge: bytes,
    stored_credential: WebAuthnCredential,
) -> webauthn.authentication.verify_authentication_response.VerifiedAuthentication:
    return webauthn.verify_authentication_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=settings.webauthn_rp_id,
        expected_origin=settings.webauthn_origin,
        credential_public_key=stored_credential.public_key,
        credential_current_sign_count=stored_credential.sign_count,
    )


def find_credential_by_raw_id(session: Session, raw_id_b64url: str) -> WebAuthnCredential | None:
    credential_id = webauthn.helpers.base64url_to_bytes(raw_id_b64url)
    return session.exec(
        select(WebAuthnCredential).where(WebAuthnCredential.credential_id == credential_id)
    ).one_or_none()


def new_user_handle() -> bytes:
    """An opaque per-registration handle. We never need to look anything up
    by it afterward — ``credential_id`` (globally unique) does that job — so
    a fresh random value per ceremony is simplest."""
    return secrets.token_bytes(16)


def utcnow() -> datetime:
    return datetime.now(UTC)
