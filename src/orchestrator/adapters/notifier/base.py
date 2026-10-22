"""Notifier interface. Channels: ntfy, e-mail (SMTP) and a generic webhook; several
can be active at once (:class:`~orchestrator.adapters.notifier.multi.MultiNotifier`).

Callers that just want "tell someone" use :meth:`Notifier.notify`. The notification
engine uses :meth:`Notifier.notify_via` so each event is delivered to the channels
its rules name, and the per-channel outcome ends up in the feed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum


class NotifySeverity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class Notifier(ABC):
    @abstractmethod
    async def notify(
        self,
        *,
        severity: NotifySeverity,
        title: str,
        body: str,
        tags: list[str] | None = None,
        approve_url: str | None = None,
    ) -> None:
        """Deliver to every channel this notifier has. Raises if delivery failed."""

    def channels(self) -> list[str]:
        """Names of the channels behind this notifier ([] = nothing will be sent)."""
        return []

    async def notify_via(
        self,
        channels: Sequence[str] | None,
        *,
        severity: NotifySeverity,
        title: str,
        body: str,
        tags: list[str] | None = None,
        approve_url: str | None = None,
    ) -> dict[str, str]:
        """Deliver to the named channels (None = all). Never raises; returns
        ``{channel: "ok" | "error: ..."}``."""
        try:
            await self.notify(
                severity=severity, title=title, body=body, tags=tags, approve_url=approve_url
            )
        except Exception as exc:
            return {"default": f"error: {exc}"}
        return {"default": "ok"}
