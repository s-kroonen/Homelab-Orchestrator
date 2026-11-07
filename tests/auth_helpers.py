"""Shared test plumbing for registering the first admin (OR-58: it needs the setup link)."""

from __future__ import annotations

from orchestrator.config import get_settings
from orchestrator.db.session import session_scope
from orchestrator.web.invites import open_setup_invite


def setup_token() -> str:
    """The open setup link's token while no admin exists ("" once one does)."""
    with session_scope() as session:
        invite = open_setup_invite(session, get_settings())
        return invite.token if invite else ""
