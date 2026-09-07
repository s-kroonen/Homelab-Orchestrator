"""No-op notifier used until phase 9 wires mailcow + ntfy."""

from __future__ import annotations

from orchestrator.adapters.notifier.base import Notifier, NotifySeverity
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class NullNotifier(Notifier):
    async def notify(
        self,
        *,
        severity: NotifySeverity,
        title: str,
        body: str,
        tags: list[str] | None = None,
        approve_url: str | None = None,
    ) -> None:
        log.info(
            "notify.null",
            severity=severity.value,
            title=title,
            tags=tags or [],
            has_approve_url=approve_url is not None,
        )
