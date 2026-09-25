"""MqttPowerAdapter against a fake broker — no real MQTT connection.

Protocol pinned here (see adapters/power/mqtt.py's module docstring for the
real device's shape): one shared command topic per target, literal payloads
on/soft/off/reset/cycle; state and availability are separate retained topics.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiomqtt
import pytest
from pydantic import SecretStr

from orchestrator.adapters.errors import AdapterUnreachable
from orchestrator.adapters.power.mqtt import MqttPowerAdapter
from orchestrator.config import Settings
from orchestrator.domain.enums import PowerState


class FakeMessage:
    def __init__(self, topic: str, payload: str) -> None:
        self.topic = topic
        self.payload = payload.encode("utf-8")


class FakeMqttClient:
    """Stands in for aiomqtt.Client: an async-context-managed message source."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []
        self.subscribed: list[str] = []
        self.fail_publish = False
        self._queue: asyncio.Queue[FakeMessage | None] = asyncio.Queue()
        #: Set to make the NEXT __anext__ raise this instead of yielding a
        #: message — simulates the broker dropping the connection.
        self._raise: BaseException | None = None

    async def __aenter__(self) -> FakeMqttClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)

    async def publish(self, topic: str, payload: Any = None, qos: int = 0, **_: Any) -> None:
        if self.fail_publish:
            raise aiomqtt.MqttError("boom")
        self.published.append((topic, payload))

    @property
    def messages(self) -> FakeMqttClient:
        return self

    def __aiter__(self) -> FakeMqttClient:
        return self

    async def __anext__(self) -> FakeMessage:
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc
        msg = await self._queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    def feed(self, topic: str, payload: str) -> None:
        self._queue.put_nowait(FakeMessage(topic, payload))


def _settings(**overrides: Any) -> Settings:
    return Settings(
        mqtt_host="broker.test",
        mqtt_power_topic_prefix="ipmi-manager",
        mqtt_password=SecretStr(""),
        **overrides,
    )


@pytest.fixture
async def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[MqttPowerAdapter, FakeMqttClient]:
    adapter = MqttPowerAdapter(_settings())
    fake = FakeMqttClient()
    monkeypatch.setattr(adapter, "_build_client", lambda: fake)
    await adapter.start()
    yield adapter, fake
    await adapter.stop()


async def _observe(
    adapter: MqttPowerAdapter, fake: FakeMqttClient, target: str, kind: str, value: str
) -> None:
    """Feed one retained message and wait until the listener has processed it.

    Clears the target's event first: asyncio.Event.wait() returns immediately
    once set, so without clearing, a second observation for a target already
    seen would not actually wait for the new message to be processed.
    """
    event = adapter._event_for(target)
    event.clear()
    fake.feed(f"ipmi-manager/{target}/{kind}", value)
    await asyncio.wait_for(event.wait(), timeout=2)


async def test_start_subscribes_to_state_and_availability_wildcards(
    wired: tuple[MqttPowerAdapter, FakeMqttClient],
) -> None:
    _, fake = wired
    assert set(fake.subscribed) == {"ipmi-manager/+/state", "ipmi-manager/+/availability"}


async def test_wake_publishes_on(wired: tuple[MqttPowerAdapter, FakeMqttClient]) -> None:
    adapter, fake = wired
    await _observe(adapter, fake, "hp-ilo2", "availability", "online")

    await adapter.wake("hp-ilo2", reason="test")

    assert fake.published == [("ipmi-manager/hp-ilo2/command", "on")]


@pytest.mark.parametrize(
    "force,expected_payload",
    [(False, "soft"), (True, "off")],
)
async def test_power_off_maps_force_to_soft_or_hard(
    wired: tuple[MqttPowerAdapter, FakeMqttClient], force: bool, expected_payload: str
) -> None:
    adapter, fake = wired
    await _observe(adapter, fake, "supermicro", "availability", "online")

    await adapter.power_off("supermicro", reason="test", force=force)

    assert fake.published == [("ipmi-manager/supermicro/command", expected_payload)]


@pytest.mark.parametrize(
    "force,expected_payload",
    [(False, "reset"), (True, "cycle")],
)
async def test_restart_maps_force_to_reset_or_cycle(
    wired: tuple[MqttPowerAdapter, FakeMqttClient], force: bool, expected_payload: str
) -> None:
    adapter, fake = wired
    await _observe(adapter, fake, "hp-ilo2", "availability", "online")

    await adapter.restart("hp-ilo2", reason="test", force=force)

    assert fake.published == [("ipmi-manager/hp-ilo2/command", expected_payload)]


async def test_command_refuses_a_target_reporting_offline(
    wired: tuple[MqttPowerAdapter, FakeMqttClient],
) -> None:
    adapter, fake = wired
    await _observe(adapter, fake, "hp-ilo2", "availability", "offline")

    with pytest.raises(AdapterUnreachable, match="offline"):
        await adapter.wake("hp-ilo2", reason="test")
    assert fake.published == []


async def test_command_still_sends_for_an_unconfirmed_target(
    wired: tuple[MqttPowerAdapter, FakeMqttClient],
) -> None:
    """A target we have never heard from is a likely config mistake (wrong
    power_mgr_target) but must not silently block the only command that could
    reveal it — it logs a warning and still publishes."""
    adapter, fake = wired

    await adapter.wake("never-seen", reason="test")

    assert fake.published == [("ipmi-manager/never-seen/command", "on")]


async def test_publish_failure_raises_adapter_unreachable(
    wired: tuple[MqttPowerAdapter, FakeMqttClient],
) -> None:
    adapter, fake = wired
    await _observe(adapter, fake, "hp-ilo2", "availability", "online")
    fake.fail_publish = True

    with pytest.raises(AdapterUnreachable):
        await adapter.wake("hp-ilo2", reason="test")


async def test_get_status_reflects_state_topic(
    wired: tuple[MqttPowerAdapter, FakeMqttClient]
) -> None:
    adapter, fake = wired
    await _observe(adapter, fake, "hp-ilo2", "state", "on")

    report = await adapter.get_status("hp-ilo2")

    assert report.state is PowerState.ON


async def test_get_status_is_unknown_when_target_reports_offline(
    wired: tuple[MqttPowerAdapter, FakeMqttClient],
) -> None:
    adapter, fake = wired
    await _observe(adapter, fake, "hp-ilo2", "state", "on")
    await _observe(adapter, fake, "hp-ilo2", "availability", "offline")

    report = await adapter.get_status("hp-ilo2")

    assert report.state is PowerState.UNKNOWN
    assert "offline" in (report.detail or "")


async def test_get_status_is_unknown_and_times_out_fast_for_an_unseen_target(
    monkeypatch: pytest.MonkeyPatch, wired: tuple[MqttPowerAdapter, FakeMqttClient]
) -> None:
    adapter, _fake = wired
    import orchestrator.adapters.power.mqtt as mqtt_module

    monkeypatch.setattr(mqtt_module, "_READY_TIMEOUT_S", 0.05)

    report = await adapter.get_status("nobody-configured-this")

    assert report.state is PowerState.UNKNOWN
    assert "power_mgr_target" in (report.detail or "")


async def test_hold_and_release_are_local_only_and_do_not_publish(
    wired: tuple[MqttPowerAdapter, FakeMqttClient],
) -> None:
    adapter, fake = wired

    handle = await adapter.hold("hp-ilo2", reason="wake in progress", ttl_s=600)
    await adapter.release("hp-ilo2", handle=handle)

    assert handle
    assert fake.published == []


async def test_reconnects_after_the_broker_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker lives inside the managed stack and can legitimately bounce —
    one MqttError must not permanently disable power control."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_: real_sleep(0))  # skip the backoff wait

    clients = [FakeMqttClient(), FakeMqttClient()]
    calls = iter(clients)
    adapter = MqttPowerAdapter(_settings())
    monkeypatch.setattr(adapter, "_build_client", lambda: next(calls))

    await adapter.start()
    await _observe(adapter, clients[0], "hp-ilo2", "availability", "online")
    assert adapter._client is clients[0]

    # Simulate the broker dropping the connection: unblock the queued get()
    # with a throwaway message, then the NEXT __anext__ call raises.
    clients[0]._raise = aiomqtt.MqttError("connection lost")
    clients[0].feed("ipmi-manager/hp-ilo2/availability", "online")

    for _ in range(50):
        await real_sleep(0)
        if adapter._client is clients[1]:
            break
    assert adapter._client is clients[1]

    await adapter.stop()
