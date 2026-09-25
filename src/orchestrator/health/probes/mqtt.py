"""MQTT heartbeat probe — has this service published recently?

State mapping, and the distinction matters as much as everywhere else:

    a message arrives within the window   ->  HEALTHY
    connected, but silence for the window ->  FAILED   (the heartbeat stopped)
    could not reach or authenticate to the broker -> UNKNOWN

Silence is a real signal: a service that is supposed to publish every N seconds
and does not is telling us something. A broker we cannot reach is not.

Worth noting for this homelab specifically: the broker lives inside the stack
being managed, so a broker outage makes every MQTT probe UNKNOWN at once. That
is the correct answer — the services may be perfectly fine and we simply cannot
see them — but it means MQTT should rarely be a service's *only* required probe.
"""

from __future__ import annotations

import asyncio
import time

from orchestrator.config import get_settings
from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import MqttProbeConfig, ProbeResult
from orchestrator.health.probes.base import Probe, register_probe
from orchestrator.health.transports.base import CommandTransport
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


@register_probe
class MqttHeartbeatProbe(Probe):
    kind = ProbeKind.MQTT

    async def run(
        self,
        row: ProbeRow,
        *,
        transport: CommandTransport | None = None,
    ) -> ProbeResult:
        try:
            cfg = MqttProbeConfig.model_validate({**row.config, "kind": ProbeKind.MQTT})
        except Exception as exc:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"probe config is invalid: {exc}",
                details={"config_error": True},
            )

        try:
            import aiomqtt
        except ImportError as exc:  # pragma: no cover - declared dependency
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"aiomqtt is not installed: {exc}",
                details={"config_error": True},
            )

        settings = get_settings()
        # Broker connection comes from settings, not from the probe config: it is
        # infrastructure, and putting a password in services.yaml would leak it
        # into the file the dashboard saves.
        host = str(row.config.get("host") or settings.mqtt_host)
        port = int(row.config.get("port") or settings.mqtt_port)
        window = cfg.within_seconds
        started = time.monotonic()
        details: dict[str, object] = {"topic": cfg.topic, "within_seconds": window}

        try:
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=settings.mqtt_username or None,
                password=settings.mqtt_password.get_secret_value() or None,
                timeout=min(window, 10),
            ) as client:
                await client.subscribe(cfg.topic)
                try:
                    await asyncio.wait_for(self._await_message(client), timeout=window)
                except TimeoutError:
                    return self._result(
                        row,
                        HealthState.FAILED,
                        message=(
                            f"no message on {cfg.topic!r} within {window}s — the "
                            f"heartbeat has stopped"
                        ),
                        latency_ms=int((time.monotonic() - started) * 1000),
                        details={**details, "silent": True},
                    )
        except Exception as exc:
            # Broker unreachable / auth rejected / TLS. We learned nothing about
            # the service itself.
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"could not observe MQTT broker {host}:{port}: {exc}",
                latency_ms=int((time.monotonic() - started) * 1000),
                details={**details, "indeterminate": True, "broker": f"{host}:{port}"},
            )

        return self._result(
            row,
            HealthState.HEALTHY,
            message=f"received a message on {cfg.topic!r}",
            latency_ms=int((time.monotonic() - started) * 1000),
            details=details,
        )

    @staticmethod
    async def _await_message(client: object) -> None:
        """Return as soon as one message arrives on any subscribed topic."""
        async for _message in client.messages:  # type: ignore[attr-defined]
            return
