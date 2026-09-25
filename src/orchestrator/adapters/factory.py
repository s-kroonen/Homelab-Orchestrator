"""Adapter selection and lifecycle. Consumed by :mod:`orchestrator.web.deps`."""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.adapters.notifier.base import Notifier
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.api import LivePbsAdapter
from orchestrator.adapters.pbs.base import PbsAdapter
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.power.base import PowerAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.power.mqtt import MqttPowerAdapter
from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter
from orchestrator.adapters.proxmox.base import ProxmoxAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.config import AdapterMode, Settings, get_settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class AdapterBundle:
    power: PowerAdapter
    proxmox: ProxmoxAdapter
    pbs: PbsAdapter
    notifier: Notifier

    async def start_all(self) -> None:
        await self.power.start()
        await self.proxmox.start()
        await self.pbs.start()

    async def stop_all(self) -> None:
        await self.power.stop()
        await self.proxmox.stop()
        await self.pbs.stop()


def build_adapters(settings: Settings | None = None) -> AdapterBundle:
    settings = settings or get_settings()

    power: PowerAdapter
    if settings.resolve_adapter(settings.power_adapter) is AdapterMode.DRY_RUN:
        power = DryRunPowerAdapter()
    elif not MqttPowerAdapter.IMPLEMENTED:
        # Phase 3 has not landed. Returning the stub here would raise from
        # start() and crash-loop the container, taking Proxmox and PBS — which
        # DO work — down with it. Substitute dry-run and say so loudly.
        log.warning(
            "adapters.power.not_implemented",
            requested=settings.power_adapter,
            using="DryRunPowerAdapter",
            impact="Wake and power-off are logged no-ops. Backups against real "
            "Proxmox/PBS still work; nodes will not actually be powered on.",
            fix="Lands in phase 3.",
        )
        power = DryRunPowerAdapter()
    else:
        power = MqttPowerAdapter(settings)

    proxmox: ProxmoxAdapter
    if settings.resolve_adapter(settings.proxmox_adapter) is AdapterMode.DRY_RUN:
        proxmox = DryRunProxmoxAdapter()
    else:
        proxmox = LiveProxmoxAdapter(settings)

    pbs: PbsAdapter
    if settings.resolve_adapter(settings.pbs_adapter) is AdapterMode.DRY_RUN:
        pbs = DryRunPbsAdapter()
    else:
        pbs = LivePbsAdapter(settings)

    # Notifier: only null exists in phase 1.
    notifier: Notifier = NullNotifier()

    log.info(
        "adapters.selected",
        power=type(power).__name__,
        proxmox=type(proxmox).__name__,
        pbs=type(pbs).__name__,
        notifier=type(notifier).__name__,
        dry_run_flag=settings.dry_run,
    )
    return AdapterBundle(power=power, proxmox=proxmox, pbs=pbs, notifier=notifier)
