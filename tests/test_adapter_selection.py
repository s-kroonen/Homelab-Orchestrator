"""DRY_RUN=true forces every adapter to its dry-run implementation."""

from __future__ import annotations

from orchestrator.adapters.factory import build_adapters
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.power.mqtt import MqttPowerAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter


def test_dry_run_selects_all_dry_run_impls() -> None:
    bundle = build_adapters()
    assert isinstance(bundle.power, DryRunPowerAdapter)
    assert isinstance(bundle.proxmox, DryRunProxmoxAdapter)
    assert isinstance(bundle.pbs, DryRunPbsAdapter)
    assert isinstance(bundle.notifier, NullNotifier)


def test_live_power_selects_the_mqtt_adapter(monkeypatch) -> None:
    """DRY_RUN=false + POWER_ADAPTER=mqtt hands out the real adapter.

    Only the type is asserted here, not start() — that opens a real socket, and
    MqttPowerAdapter's own connect/reconnect behaviour is covered against a fake
    broker in test_power_mqtt.py.
    """
    from orchestrator import config as config_module

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("POWER_ADAPTER", "mqtt")
    monkeypatch.setenv("PROXMOX_ADAPTER", "dry_run")
    monkeypatch.setenv("PBS_ADAPTER", "dry_run")
    config_module.reset_settings_cache()

    bundle = build_adapters()
    assert isinstance(bundle.power, MqttPowerAdapter)
