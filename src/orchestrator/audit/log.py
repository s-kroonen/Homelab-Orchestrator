"""Append-only audit writer.

Every privileged action goes through :func:`record` — wake requests, backup
runs, restore proposals, YAML save/reset, power-off, config changes. The
resulting rows drive both the security trail and the dashboard's activity
timeline. Callers MUST NOT update or delete audit rows from application code.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from orchestrator.db.models import AuditEntry
from orchestrator.domain.enums import AuditResult
from orchestrator.logging_setup import get_correlation_id, get_logger

log = get_logger(__name__)


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    target: str = "",
    result: AuditResult = AuditResult.OK,
    details: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> AuditEntry:
    """Insert one audit row. Caller commits (usually via ``session_scope``)."""
    entry = AuditEntry(
        actor=actor,
        action=action,
        target=target,
        result=result,
        details=details or {},
        correlation_id=correlation_id or get_correlation_id(),
    )
    session.add(entry)
    session.flush()
    log.info(
        "audit",
        actor=actor,
        action=action,
        target=target,
        result=result.value,
        entry_id=entry.id,
    )
    return entry
