"""Notifier interface. Two channels planned (mailcow + ntfy); phase 9 wires them.

Seam only in phase 1 — the DI container returns a no-op implementation so
callers can already emit alerts without knowing whether they land anywhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
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
    ) -> None: ...
