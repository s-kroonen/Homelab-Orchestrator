"""Power-manager connectivity (MQTT) and the maintenance page's wake gate."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiomqtt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.errors import AdapterUnreachable
from orchestrator.adapters.power import mqtt as mqtt_module
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.power.mqtt import MqttPowerAdapter
from orchestrator.config import Settings
from orchestrator.db import session as session_module
from orchestrator.db.models import Node, PipelineRun, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import PipelineKind, PipelineStatus
from orchestrator.web.app import create_app
from orchestrator.web.routers.infra import power_checks
from tests.test_power_mqtt import FakeMqttClient, _observe

EXAMPLE = Path(__file__).parent.parent / "config" / "services.example.yaml"


class RefusedClient:
    async def __aenter__(self) -> RefusedClient:
        raise aiomqtt.MqttError("[Errno 111] Connection refused")

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mqtt_module, "_READY_TIMEOUT_S", 0.2)


async def test_command_error_says_why_the_broker_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, quick: None
) -> None:
    adapter = MqttPowerAdapter(Settings(mqtt_host="broker.test", mqtt_port=1883))
    monkeypatch.setattr(adapter, "_build_client", lambda: RefusedClient())
    await adapter.start()
    try:
        with pytest.raises(AdapterUnreachable) as err:
            await adapter.wake("hp-ilo2", reason="t")
    finally:
        await adapter.stop()
    text = str(err.value)
    assert "MQTT connection is not up" in text
    assert "broker.test:1883" in text and "Connection refused" in text


async def test_an_unresolvable_broker_keeps_retrying_and_is_reported(
    monkeypatch: pytest.MonkeyPatch, quick: None
) -> None:
    class NoDns:
        async def __aenter__(self) -> None:
            raise OSError("[Errno -2] Name or service not known")

        async def __aexit__(self, *exc: object) -> bool:
            return False

    adapter = MqttPowerAdapter(Settings(mqtt_host="mqtt.example.lan"))
    monkeypatch.setattr(adapter, "_build_client", lambda: NoDns())
    await adapter.start()
    try:
        result = await adapter.check(["hp-ilo2"])
        assert adapter._listener_task is not None and not adapter._listener_task.done()
    finally:
        await adapter.stop()
    assert result.connected is False
    assert "Name or service not known" in result.detail


async def test_check_reports_each_target(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = MqttPowerAdapter(
        Settings(mqtt_host="broker.test", mqtt_power_topic_prefix="ipmi-manager")
    )
    fake = FakeMqttClient()
    monkeypatch.setattr(adapter, "_build_client", lambda: fake)
    monkeypatch.setattr(mqtt_module, "_READY_TIMEOUT_S", 0.2)
    await adapter.start()
    try:
        await _observe(adapter, fake, "hp-ilo2", "availability", "online")
        await _observe(adapter, fake, "hp-ilo2", "state", "on")
        await _observe(adapter, fake, "supermicro", "availability", "offline")
        result = await adapter.check(["hp-ilo2", "supermicro", "ghost"])
    finally:
        await adapter.stop()
    assert result.connected is True
    by = {t.target: t for t in result.targets}
    assert by["hp-ilo2"].online is True and by["hp-ilo2"].state.value == "on"
    assert by["supermicro"].online is False
    assert by["ghost"].online is None


def _session_with_nodes() -> Session:
    session_module.create_all_for_tests()
    session = Session(get_engine())
    session.add(Node(name="eve1", power_mgr_target="hp-ilo2"))
    session.add(Node(name="burst", power_mgr_target=""))
    session.commit()
    return session


async def test_power_checks_rows_for_a_working_and_a_broken_manager() -> None:
    session = _session_with_nodes()
    ok = await power_checks(session, SimpleNamespace(power=DryRunPowerAdapter()))
    assert [r["target"] for r in ok] == ["power_manager", "power:hp-ilo2 (eve1)"]
    assert all(r["ok"] for r in ok)

    class Down:
        async def check(self, targets: Any) -> Any:
            from orchestrator.adapters.power.base import PowerCheck

            return PowerCheck(connected=False, detail="cannot reach the broker at b:1883 (x)")

    bad = await power_checks(session, SimpleNamespace(power=Down()))
    assert len(bad) == 1
    assert bad[0]["reachable"] is False and "cannot reach the broker" in bad[0]["error"]


# --- the maintenance page honours the wake switches ------------------------------


@pytest.fixture
def app_with_registry(monkeypatch: pytest.MonkeyPatch) -> Any:
    from orchestrator.config import get_settings

    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    shutil.copy(EXAMPLE, get_settings().services_yaml_path)
    return create_app()


def _flip(app: Any, *, service: bool | None = None, node: bool | None = None) -> None:
    with Session(get_engine()) as session:
        svc = session.exec(select(Service).where(Service.slug == "example-media")).one()
        if service is not None:
            svc.wake_on_hit_enabled = service
            session.add(svc)
        if node is not None:
            n = session.get(Node, svc.node_id)
            n.wake_on_hit_enabled = node
            session.add(n)
        session.commit()


@pytest.mark.parametrize("which", ["service", "node"])
def test_wake_disabled_shows_unavailable_and_starts_nothing(
    app_with_registry: Any, which: str
) -> None:
    with TestClient(app_with_registry) as client:
        _flip(app_with_registry, **{which: False})
        r = client.get("/maintenance/example-media")
        with Session(get_engine()) as session:
            runs = session.exec(select(PipelineRun)).all()
    assert r.status_code == 200
    assert "is not available" in r.text
    assert "Automatic start is turned off" in r.text
    assert "Try again" not in r.text
    assert runs == []


def test_a_wake_already_in_flight_is_still_shown_when_disabled(app_with_registry: Any) -> None:
    with TestClient(app_with_registry) as client:
        _flip(app_with_registry, service=False)
        with Session(get_engine()) as session:
            svc = session.exec(select(Service).where(Service.slug == "example-media")).one()
            session.add(
                PipelineRun(
                    kind=PipelineKind.WAKE,
                    correlation_id="test-run",
                    service_id=svc.id,
                    status=PipelineStatus.RUNNING,
                    steps=[{"name": "node_power", "status": "running"}],
                )
            )
            session.commit()
        r = client.get("/maintenance/example-media")
    assert "Waking" in r.text
    assert "is not available" not in r.text
