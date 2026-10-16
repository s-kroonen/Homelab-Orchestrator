"""DRY_RUN=true forces every adapter to its dry-run implementation."""

from __future__ import annotations

from orchestrator.adapters.factory import build_adapters
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.power.mqtt import MqttPowerAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter


def test_dry_run_selects_all_dry_run_impls() -> None:
    bundle = build_adapters()
    assert isinstance(bundle.power, DryRunPowerAdapter)
    assert isinstance(bundle.proxmox, DryRunProxmoxAdapter)
    assert isinstance(bundle.pbs, DryRunPbsAdapter)
    assert isinstance(bundle.proxy, DryRunProxyAdapter)
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


def test_live_proxy_selects_the_traefik_manager_adapter(monkeypatch) -> None:
    """DRY_RUN=false + PROXY_ADAPTER=traefik_manager + TRAEFIK_MANAGER_URL
    hands out the real adapter. Only the type is asserted — its own REST
    calls are covered against a mocked API in
    test_proxy_adapter_traefik_manager.py."""
    from orchestrator import config as config_module

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("POWER_ADAPTER", "dry_run")
    monkeypatch.setenv("PROXMOX_ADAPTER", "dry_run")
    monkeypatch.setenv("PBS_ADAPTER", "dry_run")
    monkeypatch.setenv("PROXY_ADAPTER", "traefik_manager")
    monkeypatch.setenv("TRAEFIK_MANAGER_URL", "http://tm.test:8080")
    config_module.reset_settings_cache()

    bundle = build_adapters()
    assert isinstance(bundle.proxy, TraefikManagerProxyAdapter)


def test_live_proxy_falls_back_to_dry_run_when_url_unset(monkeypatch) -> None:
    """A live PROXY_ADAPTER with TRAEFIK_MANAGER_URL left blank is a footgun
    (every call would just degrade to "unconfigured" anyway) — the factory
    substitutes DryRunProxyAdapter and logs loudly, same "substitute + warn"
    convention MqttPowerAdapter.IMPLEMENTED uses for a not-yet-landed power
    adapter. ORCHESTRATOR_PUBLIC_URL being unset does NOT trigger this —
    only TRAEFIK_MANAGER_URL does (see build_adapters)."""
    from orchestrator import config as config_module

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("POWER_ADAPTER", "dry_run")
    monkeypatch.setenv("PROXMOX_ADAPTER", "dry_run")
    monkeypatch.setenv("PBS_ADAPTER", "dry_run")
    monkeypatch.setenv("PROXY_ADAPTER", "traefik_manager")
    config_module.reset_settings_cache()

    bundle = build_adapters()
    assert isinstance(bundle.proxy, DryRunProxyAdapter)
