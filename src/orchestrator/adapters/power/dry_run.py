"""Log-only power adapter for tests, dev, and DRY_RUN mode."""

from __future__ import annotations

import uuid

from orchestrator.adapters.power.base import NodeReport, PowerAdapter
from orchestrator.domain.enums import PowerState
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class DryRunPowerAdapter(PowerAdapter):
    def __init__(self) -> None:
        self._simulated_state: dict[str, PowerState] = {}
        self._holds: dict[str, set[str]] = {}

    async def start(self) -> None:
        log.info("power.dry_run.start")

    async def stop(self) -> None:
        log.info("power.dry_run.stop")

    async def wake(self, node_name: str, *, reason: str) -> None:
        log.info("power.dry_run.wake", node=node_name, reason=reason)
        self._simulated_state[node_name] = PowerState.ON

    async def power_off(self, node_name: str, *, reason: str, force: bool = False) -> None:
        log.info("power.dry_run.power_off", node=node_name, reason=reason, force=force)
        self._simulated_state[node_name] = PowerState.OFF

    async def restart(self, node_name: str, *, reason: str, force: bool = False) -> None:
        log.info("power.dry_run.restart", node=node_name, reason=reason, force=force)
        self._simulated_state[node_name] = PowerState.BOOTING

    async def hold(self, node_name: str, *, reason: str, ttl_s: int) -> str:
        handle = uuid.uuid4().hex[:10]
        self._holds.setdefault(node_name, set()).add(handle)
        log.info(
            "power.dry_run.hold",
            node=node_name,
            reason=reason,
            ttl_s=ttl_s,
            handle=handle,
        )
        return handle

    async def release(self, node_name: str, *, handle: str) -> None:
        self._holds.get(node_name, set()).discard(handle)
        log.info("power.dry_run.release", node=node_name, handle=handle)

    async def get_status(self, node_name: str) -> NodeReport:
        state = self._simulated_state.get(node_name, PowerState.UNKNOWN)
        return NodeReport(node_name=node_name, state=state, detail="dry_run")
