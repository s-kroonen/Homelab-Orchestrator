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
from orchestrator.adapters.proxy.base import ProxyAdapter
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter
from orchestrator.config import AdapterMode, Settings, get_settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class AdapterBundle:
    power: PowerAdapter
    proxmox: ProxmoxAdapter
    pbs: PbsAdapter
    proxy: ProxyAdapter
    notifier: Notifier

    async def start_all(self) -> None:
        await self.power.start()
        await self.proxmox.start()
        await self.pbs.start()
        await self.proxy.start()

    async def stop_all(self) -> None:
        await self.power.stop()
        await self.proxmox.stop()
        await self.pbs.stop()
        await self.proxy.stop()


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

    proxy: ProxyAdapter
    if settings.resolve_adapter(settings.proxy_adapter) is AdapterMode.DRY_RUN:
        proxy = DryRunProxyAdapter()
    elif not settings.traefik_manager_url.strip():
        # A live proxy_adapter needs TRAEFIK_MANAGER_URL to do anything at
        # all — getting PROXY_ADAPTER and the URL in sync is an easy
        # footgun: the operator sets PROXY_ADAPTER=traefik_manager but
        # forgets the URL (or vice versa). Rather than handing back an
        # adapter whose every call immediately degrades to "unconfigured"
        # anyway (list_routes returns None, ensure/remove_maintenance_
        # fallback return []), substitute dry-run and say so loudly — same
        # "substitute + warn" convention MqttPowerAdapter.IMPLEMENTED uses
        # above for a not-yet-landed power adapter. ORCHESTRATOR_PUBLIC_URL
        # does NOT gate this: it only affects the maintenance-fallback
        # write path, which already no-ops gracefully on its own when
        # that's unset (see TraefikManagerProxyAdapter.
        # ensure_maintenance_fallback) — drift detection and Resync work
        # fine without it.
        log.warning(
            "adapters.proxy.not_configured",
            requested=settings.proxy_adapter,
            using="DryRunProxyAdapter",
            impact="Drift detection reads as 'unknown', Resync answers 503, and "
            "the maintenance-page fallback middleware is never injected. "
            "Every other feature is unaffected.",
            fix="Set TRAEFIK_MANAGER_URL (and ORCHESTRATOR_PUBLIC_URL, for the "
            "maintenance-page fallback specifically).",
        )
        proxy = DryRunProxyAdapter()
    else:
        proxy = TraefikManagerProxyAdapter(settings)

    # Notifier: only null exists in phase 1.
    notifier: Notifier = NullNotifier()

    log.info(
        "adapters.selected",
        power=type(power).__name__,
        proxmox=type(proxmox).__name__,
        pbs=type(pbs).__name__,
        proxy=type(proxy).__name__,
        notifier=type(notifier).__name__,
        dry_run_flag=settings.dry_run,
    )
    return AdapterBundle(power=power, proxmox=proxmox, pbs=pbs, proxy=proxy, notifier=notifier)
