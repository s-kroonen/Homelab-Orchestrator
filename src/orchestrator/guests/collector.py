"""Read every guest on time, without letting a slow one hold up the rest.

* at most ``concurrency`` guests at once, each bounded by ``per_guest_timeout_s``;
* a guest whose agent keeps not answering is read only every third cycle until it answers
  again (it is still read at once by *Read now*), so a hung agent costs little;
* the OS is read once a day (``os_every_s``), everything else every cycle.

The collector knows nothing about Proxmox: it is handed a provider and the guests to read.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from orchestrator.guests.model import (
    AgentState,
    GuestFacts,
    GuestFactsProvider,
    GuestRef,
    GuestTarget,
    now_iso,
    parse_time,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: A guest that missed this many reads in a row is read every ``_BACKOFF_EVERY`` cycles.
_BACKOFF_AFTER = 2
_BACKOFF_EVERY = 3


class GuestCollector:
    def __init__(
        self,
        provider: GuestFactsProvider,
        *,
        concurrency: int = 4,
        per_guest_timeout_s: float = 30.0,
        os_every_s: int = 86400,
    ) -> None:
        self._provider = provider
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._timeout = per_guest_timeout_s
        self._os_every_s = os_every_s

    async def collect(
        self, targets: list[GuestTarget], previous: dict[GuestRef, GuestFacts]
    ) -> list[GuestFacts]:
        """One reading per target, in the targets' order."""
        return list(
            await asyncio.gather(*(self._cycle_read(t, previous.get(t.ref)) for t in targets))
        )

    async def read_one(self, target: GuestTarget, previous: GuestFacts | None) -> GuestFacts:
        """Read one guest now, back-off or not, OS included (*Read now*)."""
        return await self._read(target, previous, os_due=True)

    async def _cycle_read(self, target: GuestTarget, previous: GuestFacts | None) -> GuestFacts:
        if previous is not None and self._backing_off(target, previous):
            return replace(previous, skipped=previous.skipped + 1, running=target.running)
        return await self._read(target, previous, os_due=self._os_due(previous))

    @staticmethod
    def _backing_off(target: GuestTarget, previous: GuestFacts) -> bool:
        return (
            target.running
            and previous.agent is AgentState.NOT_ANSWERING
            and previous.misses >= _BACKOFF_AFTER
            and previous.skipped < _BACKOFF_EVERY - 1
        )

    def _os_due(self, previous: GuestFacts | None) -> bool:
        read = parse_time(previous.os_read_at) if previous else None
        return read is None or (datetime.now(UTC) - read).total_seconds() >= self._os_every_s

    async def _read(
        self, target: GuestTarget, previous: GuestFacts | None, *, os_due: bool
    ) -> GuestFacts:
        async with self._sem:
            try:
                facts = await asyncio.wait_for(
                    self._provider.read(target, previous, os_due=os_due), self._timeout
                )
            except TimeoutError:
                facts = GuestFacts(
                    ref=target.ref,
                    name=target.name,
                    kind=target.kind,
                    running=target.running,
                    read_at=now_iso(),
                    agent=AgentState.NOT_ANSWERING,
                    agent_detail=f"no answer within {self._timeout:g} s",
                    misses=(previous.misses if previous else 0) + 1,
                )
                if previous is not None:
                    facts.addresses, facts.address_source = (
                        list(previous.addresses),
                        previous.address_source,
                    )
                    facts.os_name, facts.os_version = previous.os_name, previous.os_version
                    facts.kernel, facts.os_read_at = previous.kernel, previous.os_read_at
            except Exception as exc:  # a provider must not raise; be safe anyway
                log.warning("guests.collect.failed", guest=str(target.ref), error=str(exc))
                facts = GuestFacts(
                    ref=target.ref,
                    name=target.name,
                    kind=target.kind,
                    running=target.running,
                    read_at=now_iso(),
                    agent_detail=f"{type(exc).__name__}: {exc}",
                )
        facts.skipped = 0
        return facts


__all__ = ["GuestCollector"]
