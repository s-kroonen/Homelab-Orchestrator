"""Invite links: the only way to register a passkey without being signed in (OR-58).

* **An invite for a new admin** is made by a signed-in admin (Settings → Admins & devices) or
  by ``orchestrator-cli admin invite``.
* **An invite for an existing account** (``orchestrator-cli admin invite --for NAME``) adds a
  passkey to that admin, for when they lost every device.
* **The setup link** is the invite that lets the first admin register. It is no longer open to
  whoever loads ``/login`` first while no admin exists: the dashboard is on the internet. The
  orchestrator prints it in its log at start (while no admin exists), and ``admin reset`` /
  ``admin invite`` print one on the host.

Every invite is a ``PendingApproval`` row (``kind=admin_invite``) that works once and expires.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, func, select

from orchestrator.config import Settings
from orchestrator.db.models import Admin, PendingApproval
from orchestrator.domain.enums import ApprovalKind, ApprovalStatus

#: How long a link printed on the host (``admin reset``, ``admin invite``) stays valid.
CLI_LINK_TTL = timedelta(minutes=30)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """SQLite hands datetimes back naive; they were written in UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def admin_count(session: Session) -> int:
    """Every admin, disabled ones included, so a disabled account never reopens setup."""
    return int(session.exec(select(func.count()).select_from(Admin)).one())


def invite_url(settings: Settings, token: str) -> str:
    return f"{settings.webauthn_origin.rstrip('/')}/login?invite={token}"


def create_invite(
    session: Session,
    *,
    requested_by: str,
    ttl: timedelta,
    label: str = "",
    setup: bool = False,
    for_admin: Admin | None = None,
) -> PendingApproval:
    payload: dict[str, Any] = {"label": label}
    if setup:
        payload["setup"] = True
    if for_admin is not None:
        payload["admin_id"] = for_admin.id
        payload["username"] = for_admin.username
    invite = PendingApproval(
        kind=ApprovalKind.ADMIN_INVITE,
        token=secrets.token_urlsafe(32),
        payload=payload,
        requested_by=requested_by,
        expires_at=_utcnow() + ttl,
    )
    session.add(invite)
    session.flush()
    return invite


def find_open_invite(session: Session, token: str) -> PendingApproval | None:
    """The pending, unexpired invite with this token, or None. A setup link counts only while
    no admin exists."""
    if not token:
        return None
    invite = session.exec(
        select(PendingApproval).where(
            PendingApproval.token == token,
            PendingApproval.kind == ApprovalKind.ADMIN_INVITE,
        )
    ).one_or_none()
    if invite is None or invite.status is not ApprovalStatus.PENDING:
        return None
    if _aware(invite.expires_at) < _utcnow():
        return None
    if (invite.payload or {}).get("setup") and admin_count(session) > 0:
        return None  # a setup link is for the first admin only; another one may be in the log
    return invite


def invite_target(session: Session, invite: PendingApproval) -> Admin | None:
    """The existing admin an invite adds a passkey to; None for an invite for a new admin."""
    admin_id = (invite.payload or {}).get("admin_id")
    return session.get(Admin, admin_id) if admin_id is not None else None


def open_setup_invite(session: Session, settings: Settings) -> PendingApproval | None:
    """While no admin exists: the open setup link, made if there is none. None once one does.

    Called at start, so a fresh install prints the link in its log; an open one is reused, so a
    restart prints the same link until it expires."""
    if admin_count(session) > 0:
        return None
    for invite in session.exec(
        select(PendingApproval).where(
            PendingApproval.kind == ApprovalKind.ADMIN_INVITE,
            PendingApproval.status == ApprovalStatus.PENDING,
        )
    ).all():
        if (invite.payload or {}).get("setup") and _aware(invite.expires_at) >= _utcnow():
            return invite
    return create_invite(
        session,
        requested_by="system",
        ttl=timedelta(hours=settings.invite_ttl_hours),
        label="setup",
        setup=True,
    )


def cancel_open_invites(session: Session) -> int:
    """Expire every open invite (``admin reset``: a link handed out before must not become the
    way in once nobody is left to notice). Returns how many were open."""
    rows = session.exec(
        select(PendingApproval).where(
            PendingApproval.kind == ApprovalKind.ADMIN_INVITE,
            PendingApproval.status == ApprovalStatus.PENDING,
        )
    ).all()
    for invite in rows:
        invite.status = ApprovalStatus.EXPIRED
        session.add(invite)
    return len(rows)


__all__ = [
    "CLI_LINK_TTL",
    "admin_count",
    "cancel_open_invites",
    "create_invite",
    "find_open_invite",
    "invite_target",
    "invite_url",
    "open_setup_invite",
]
