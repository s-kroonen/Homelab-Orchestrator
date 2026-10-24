"""Portainer probe - is a stack (or one container) up, and are its own health checks passing?

    stack missing / stopped, a container not running or unhealthy   ->  FAILED
    a container still starting                                      ->  UNKNOWN (not yet)
    Portainer unreachable / key refused / not set up                ->  UNKNOWN (we could not ask)
    every container running, none unhealthy or starting             ->  HEALTHY

It needs no SSH or host agent: Portainer already knows. It only says the containers are up; an
HTTP probe through the proxy host says the application answers.
"""

from __future__ import annotations

import time

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.portainer.base import PortainerAdapter
from orchestrator.adapters.portainer.shared import get_shared
from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult
from orchestrator.health.probes.base import Probe, register_probe
from orchestrator.health.transports.base import CommandTransport


@register_probe
class PortainerProbe(Probe):
    kind = ProbeKind.PORTAINER

    async def run(self, row: ProbeRow, *, transport: CommandTransport | None = None) -> ProbeResult:
        cfg = row.config or {}
        stack, container = str(cfg.get("stack") or ""), str(cfg.get("container") or "")
        try:
            endpoint = int(cfg["endpoint"])
        except (KeyError, TypeError, ValueError):
            return self._config_error(row, "needs 'endpoint' (the Portainer environment number)")
        if bool(stack) == bool(container):
            return self._config_error(row, "needs exactly one of 'stack' or 'container'")

        adapter = get_shared()
        own: PortainerAdapter | None = None
        if adapter is None:
            from orchestrator.adapters.factory import build_adapters
            from orchestrator.config import get_settings

            own = adapter = build_adapters(get_settings()).portainer
        assert adapter is not None
        started = time.monotonic()
        try:
            if not adapter.configured:
                return self._result(
                    row, HealthState.UNKNOWN, message="Portainer is not set up", details={}
                )
            return await self._check(row, adapter, endpoint, stack, container, started)
        except AdapterError as exc:
            return self._result(row, HealthState.UNKNOWN, message=str(exc))
        finally:
            if own is not None:
                await own.stop()

    def _config_error(self, row: ProbeRow, message: str) -> ProbeResult:
        return self._result(
            row,
            HealthState.UNKNOWN,
            message=f"portainer probe {message}",
            details={"config_error": True},
        )

    async def _check(
        self,
        row: ProbeRow,
        adapter: PortainerAdapter,
        endpoint: int,
        stack: str,
        container: str,
        started: float,
    ) -> ProbeResult:
        def done(state: HealthState, message: str) -> ProbeResult:
            return self._result(
                row, state, message=message, latency_ms=int((time.monotonic() - started) * 1000)
            )

        if stack:
            found = await adapter.get_stack(endpoint, stack)
            if found is None:
                return done(
                    HealthState.FAILED, f"stack {stack!r} not found on environment {endpoint}"
                )
            if found.status != "active":
                return done(HealthState.FAILED, f"stack {stack!r} is stopped")
            boxes = await adapter.list_containers(endpoint, stack=stack)
            what = f"stack {stack!r}"
        else:
            detail = await adapter.get_container(endpoint, container)
            if detail is None:
                return done(
                    HealthState.FAILED,
                    f"container {container!r} not found on environment {endpoint}",
                )
            boxes = [detail.container]
            what = f"container {container!r}"
        if not boxes:
            return done(HealthState.FAILED, f"{what} has no containers")
        down = [c.name for c in boxes if c.state != "running"]
        if down:
            return done(HealthState.FAILED, f"not running in {what}: {', '.join(down)}")
        sick = [c.name for c in boxes if c.health == "unhealthy"]
        if sick:
            return done(HealthState.FAILED, f"unhealthy in {what}: {', '.join(sick)}")
        starting = [c.name for c in boxes if c.health == "starting"]
        if starting:
            return done(HealthState.UNKNOWN, f"still starting in {what}: {', '.join(starting)}")
        return done(HealthState.HEALTHY, f"{what}: {len(boxes)} container(s) running")
