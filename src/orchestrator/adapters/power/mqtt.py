"""MQTT client to the existing power manager.

The real device here is a Home Assistant MQTT integration in front of IPMI/iLO
(``ipmi-manager``). Its protocol is simpler than standard HA switch/button
discovery suggests: every action for one node's BMC — power on, graceful
shutdown, hard off, reset, power-cycle — is a single literal payload published
to **one shared command topic**, not a distinct command topic per entity:

    {MQTT_POWER_TOPIC_PREFIX}/{node_name}/command   <- publish here
        payload "on"     power on
        payload "soft"   graceful (ACPI) shutdown request
        payload "off"    immediate hard power off
        payload "reset"  reset line (no OS involvement — this is the manager's
                          only restart action; there is no graceful reboot)
        payload "cycle"  power off, then on — for a node reset didn't recover

    {MQTT_POWER_TOPIC_PREFIX}/{node_name}/state          <- subscribe, retained
        "on" | "off"
    {MQTT_POWER_TOPIC_PREFIX}/{node_name}/availability   <- subscribe, retained
        "online" | "offline"   (the BMC/manager's own reachability)

``node_name`` here is ``Node.power_mgr_target`` — the manager's own device id
(e.g. ``hp-ilo2``, ``supermicro``), NOT the Proxmox node name. Getting that
config wrong is silent at the MQTT layer (nothing refuses a publish to a topic
no one subscribes to), so every command logs a warning if it has never seen an
availability message for the target.

``MQTT_POWER_TOPIC_PREFIX`` must be the manager's runtime prefix
(``ipmi-manager``), not its Home Assistant *discovery* prefix
(``homeassistant/ipmi-manager/...`` — that only carries the entity-config
payloads, not state/command).

No inhibit-shutdown concept exists in this protocol, so ``hold``/``release``
are local no-ops — see :meth:`hold`.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import aiomqtt

from orchestrator.adapters.errors import AdapterUnreachable
from orchestrator.adapters.power.base import NodeReport, PowerAdapter
from orchestrator.config import Settings
from orchestrator.domain.enums import PowerState
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

_STATE_PAYLOAD: dict[str, PowerState] = {"on": PowerState.ON, "off": PowerState.OFF}

# How long a command / status query waits for the connection (or a target's
# first retained message) before giving up. Retained messages normally arrive
# within milliseconds of subscribing — this only matters right after start(),
# or when the broker is genuinely unreachable.
_READY_TIMEOUT_S = 5.0


class MqttPowerAdapter(PowerAdapter):
    IMPLEMENTED = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._prefix = settings.mqtt_power_topic_prefix.rstrip("/")

        self._client: aiomqtt.Client | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._stopping = False

        # Populated by retained messages as they arrive. Keyed by power_mgr_target.
        self._power_state: dict[str, PowerState] = {}
        self._available: dict[str, bool] = {}
        self._seen: dict[str, asyncio.Event] = {}

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if self._listener_task is not None:
            return  # idempotent
        self._stopping = False
        self._listener_task = asyncio.create_task(self._run(), name="power.mqtt.listener")
        # Give the first connection attempt a moment so a command issued right
        # after start() doesn't immediately fail. Not required for correctness:
        # every command/query below waits for this itself too.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._connected.wait(), timeout=_READY_TIMEOUT_S)
        log.info(
            "power.mqtt.start", host=self._settings.mqtt_host, connected=self._connected.is_set()
        )

    async def stop(self) -> None:
        self._stopping = True
        task, self._listener_task = self._listener_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._connected.clear()
        self._client = None
        log.info("power.mqtt.stop")

    async def _run(self) -> None:
        """Connect, subscribe, consume messages — and reconnect on failure.

        The broker runs inside the stack this orchestrator manages, so it can
        legitimately bounce. A wake request landing during a broker restart
        must not permanently disable power control until the process restarts.
        """
        backoff = 1.0
        while not self._stopping:
            try:
                async with self._build_client() as client:
                    self._client = client
                    await client.subscribe(f"{self._prefix}/+/state", qos=1)
                    await client.subscribe(f"{self._prefix}/+/availability", qos=1)
                    self._connected.set()
                    backoff = 1.0
                    async for message in client.messages:
                        self._handle_message(message)
            except aiomqtt.MqttError as exc:
                self._connected.clear()
                self._client = None
                if self._stopping:
                    return
                log.warning("power.mqtt.disconnected", error=str(exc), retry_in_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                self._connected.clear()
                self._client = None
                raise

    def _build_client(self) -> aiomqtt.Client:
        s = self._settings
        kwargs: dict[str, Any] = {}
        if s.mqtt_tls:
            kwargs["tls_params"] = aiomqtt.TLSParameters()
        return aiomqtt.Client(
            hostname=s.mqtt_host,
            port=s.mqtt_port,
            username=s.mqtt_username or None,
            password=s.mqtt_password.get_secret_value() or None,
            timeout=10,
            **kwargs,
        )

    def _handle_message(self, message: aiomqtt.Message) -> None:
        # Subscribed as f"{prefix}/+/state" and f"{prefix}/+/availability", so
        # the topic's last two segments are always (target, kind) regardless of
        # how many segments the configured prefix itself has.
        parts = str(message.topic).split("/")
        if len(parts) < 2:
            return
        target, kind = parts[-2], parts[-1]

        payload = message.payload
        text = (
            payload.decode("utf-8", "replace").strip()
            if isinstance(payload, bytes | bytearray)
            else str(payload).strip()
        )

        if kind == "state":
            self._power_state[target] = _STATE_PAYLOAD.get(text.lower(), PowerState.UNKNOWN)
        elif kind == "availability":
            self._available[target] = text.lower() == "online"
        else:
            return
        self._event_for(target).set()

    def _event_for(self, target: str) -> asyncio.Event:
        return self._seen.setdefault(target, asyncio.Event())

    # -- commands ---------------------------------------------------------------

    async def wake(self, node_name: str, *, reason: str) -> None:
        await self._command(node_name, "on", reason=reason, action="wake")

    async def power_off(self, node_name: str, *, reason: str, force: bool = False) -> None:
        payload, action = ("off", "power_off_hard") if force else ("soft", "power_off_soft")
        await self._command(node_name, payload, reason=reason, action=action)

    async def restart(self, node_name: str, *, reason: str, force: bool = False) -> None:
        payload, action = ("cycle", "power_cycle") if force else ("reset", "restart")
        await self._command(node_name, payload, reason=reason, action=action)

    async def _command(self, node_name: str, payload: str, *, reason: str, action: str) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._connected.wait(), timeout=_READY_TIMEOUT_S)
        client = self._client
        if client is None:
            raise AdapterUnreachable(
                f"power manager MQTT connection is not up (target={node_name!r}, "
                f"action={action!r})"
            )

        if self._available.get(node_name) is False:
            raise AdapterUnreachable(
                f"power target {node_name!r} reports offline in the power manager — "
                f"refusing to send {action!r}"
            )
        if node_name not in self._available:
            log.warning(
                "power.mqtt.target_unconfirmed",
                node=node_name,
                action=action,
                hint=(
                    "no availability message ever seen for this power_mgr_target — check "
                    "it matches the manager's device id (e.g. hp-ilo2, supermicro), not "
                    "the Proxmox node name"
                ),
            )

        topic = f"{self._prefix}/{node_name}/command"
        try:
            await client.publish(topic, payload=payload, qos=1)
        except aiomqtt.MqttError as exc:
            raise AdapterUnreachable(f"could not publish to {topic}: {exc}") from exc
        log.info(
            "power.mqtt.command", node=node_name, action=action, reason=reason, payload=payload
        )

    # -- holds --------------------------------------------------------------

    async def hold(self, node_name: str, *, reason: str, ttl_s: int) -> str:
        # This manager has no inhibit-shutdown mechanism to assert against — it
        # is a dumb power switch. Return a handle so callers that track their own
        # NodeState.hold_count keep working; nothing is sent over MQTT.
        handle = uuid.uuid4().hex[:10]
        log.info(
            "power.mqtt.hold_noop",
            node=node_name,
            reason=reason,
            ttl_s=ttl_s,
            handle=handle,
            note="this power manager has no inhibit-shutdown concept; tracked only in our own DB",
        )
        return handle

    async def release(self, node_name: str, *, handle: str) -> None:
        log.info("power.mqtt.release_noop", node=node_name, handle=handle)

    # -- status ---------------------------------------------------------------

    async def get_status(self, node_name: str) -> NodeReport:
        if node_name not in self._power_state and node_name not in self._available:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._event_for(node_name).wait(), timeout=_READY_TIMEOUT_S)

        if self._available.get(node_name) is False:
            return NodeReport(
                node_name=node_name,
                state=PowerState.UNKNOWN,
                detail=f"{node_name!r} reports offline in the power manager",
            )

        state = self._power_state.get(node_name)
        if state is None:
            return NodeReport(
                node_name=node_name,
                state=PowerState.UNKNOWN,
                detail=(
                    f"no state ever observed for power_mgr_target={node_name!r} — check it "
                    f"matches the manager's device id (e.g. hp-ilo2, supermicro), not the "
                    f"Proxmox node name"
                ),
            )
        return NodeReport(node_name=node_name, state=state)
