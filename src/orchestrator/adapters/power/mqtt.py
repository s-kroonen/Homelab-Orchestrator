"""MQTT client to the existing power manager. Phase 3 fills this in.

Left as a stub in phase 1 so the DI wiring already resolves it when
``POWER_ADAPTER=mqtt`` is picked in a non-dry-run environment.
"""

from __future__ import annotations

from orchestrator.adapters.power.base import NodeReport, PowerAdapter
from orchestrator.config import Settings


class MqttPowerAdapter(PowerAdapter):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def start(self) -> None:
        raise NotImplementedError("MqttPowerAdapter is implemented in phase 3.")

    async def stop(self) -> None:
        raise NotImplementedError("MqttPowerAdapter is implemented in phase 3.")

    async def wake(self, node_name: str, *, reason: str) -> None:
        raise NotImplementedError

    async def power_off(self, node_name: str, *, reason: str) -> None:
        raise NotImplementedError

    async def hold(self, node_name: str, *, reason: str, ttl_s: int) -> str:
        raise NotImplementedError

    async def release(self, node_name: str, *, handle: str) -> None:
        raise NotImplementedError

    async def get_status(self, node_name: str) -> NodeReport:
        raise NotImplementedError
