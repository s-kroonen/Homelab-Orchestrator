"""Fan-out to several channels, with per-channel outcomes."""

from __future__ import annotations

from collections.abc import Sequence

from orchestrator.adapters.notifier.base import Notifier, NotifySeverity
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class MultiNotifier(Notifier):
    def __init__(self, channels: dict[str, Notifier]) -> None:
        self._channels = channels

    def channels(self) -> list[str]:
        return list(self._channels)

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
        wanted = list(self._channels) if not channels else list(channels)
        out: dict[str, str] = {}
        for name in wanted:
            channel = self._channels.get(name)
            if channel is None:
                out[name] = "error: channel is not configured"
                continue
            try:
                await channel.notify(
                    severity=severity,
                    title=title,
                    body=body,
                    tags=tags,
                    approve_url=approve_url,
                )
                out[name] = "ok"
            except Exception as exc:  # one dead channel never blocks the others
                log.warning("notify.channel_failed", channel=name, error=str(exc))
                out[name] = f"error: {exc}"[:300]
        return out

    async def notify(
        self,
        *,
        severity: NotifySeverity,
        title: str,
        body: str,
        tags: list[str] | None = None,
        approve_url: str | None = None,
    ) -> None:
        results = await self.notify_via(
            None, severity=severity, title=title, body=body, tags=tags, approve_url=approve_url
        )
        failed = {k: v for k, v in results.items() if v != "ok"}
        if results and len(failed) == len(results):
            raise RuntimeError("; ".join(f"{k}: {v}" for k, v in failed.items()))
