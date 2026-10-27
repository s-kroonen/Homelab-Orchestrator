"""Adapter selection and lifecycle. Consumed by :mod:`orchestrator.web.deps`."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from orchestrator.adapters.notifier.base import Notifier
from orchestrator.adapters.notifier.channels import NtfyNotifier, SmtpNotifier, WebhookNotifier
from orchestrator.adapters.notifier.multi import MultiNotifier
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.api import LivePbsAdapter
from orchestrator.adapters.pbs.base import PbsAdapter
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.portainer.api import LivePortainerAdapter
from orchestrator.adapters.portainer.base import PortainerAdapter
from orchestrator.adapters.portainer.dry_run import DryRunPortainerAdapter
from orchestrator.adapters.portainer.shared import set_shared as set_shared_portainer
from orchestrator.adapters.power.base import PowerAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.power.mqtt import MqttPowerAdapter
from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter
from orchestrator.adapters.proxmox.base import ProxmoxAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.adapters.proxy.base import ProxyAdapter
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter
from orchestrator.adapters.switch import Switchable
from orchestrator.config import AdapterMode, Settings, get_settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class AdapterBundle:
    """The adapters in use. ``power`` .. ``notifier`` are :class:`Switchable` handles: they keep
    working when a connection is edited and :meth:`reload` puts new adapters behind them."""

    power: PowerAdapter
    proxmox: ProxmoxAdapter
    pbs: PbsAdapter
    proxy: ProxyAdapter
    notifier: Notifier
    #: Optional: absent in bundles built by hand (tests); always set by build_adapters. This is
    #: the DEFAULT Portainer; ``portainers`` has every connection by id.
    portainer: PortainerAdapter | None = None
    portainers: dict[str, PortainerAdapter] = field(default_factory=dict)
    default_portainer: str = ""

    def __post_init__(self) -> None:
        if self.portainer is not None and not self.portainers:
            self.default_portainer = self.default_portainer or "portainer"
            self.portainers = {self.default_portainer: self.portainer}

    def portainer_for(self, instance: str | None = None) -> PortainerAdapter | None:
        """The Portainer a connection id names (None/blank: the default one)."""
        if not instance:
            return self.portainers.get(self.default_portainer) or self.portainer
        return self.portainers.get(instance)

    async def start_all(self) -> None:
        await self.power.start()
        await self.proxmox.start()
        await self.pbs.start()
        await self.proxy.start()
        for adapter in self.portainers.values():
            await adapter.start()

    async def stop_all(self) -> None:
        await self.power.stop()
        await self.proxmox.stop()
        await self.pbs.stop()
        await self.proxy.stop()
        for adapter in self.portainers.values():
            await adapter.stop()

    async def reload(self, settings: Settings | None = None) -> None:
        """Rebuild every adapter from the current settings and put them behind the same handles
        (used after a connection is added, edited or removed). The old ones are stopped last."""
        settings = settings or get_settings()
        fresh = _build_raw(settings)
        for adapter in (
            fresh.power,
            fresh.proxmox,
            fresh.pbs,
            fresh.proxy,
            *fresh.portainers.values(),
        ):
            await adapter.start()
        old: list[Any] = []
        for name in ("power", "proxmox", "pbs", "proxy", "notifier"):
            handle = getattr(self, name)
            old.append(handle.swap(getattr(fresh, name)))
        old.extend(self.portainers.values())
        self.portainers = fresh.portainers
        self.default_portainer = fresh.default_portainer
        self.portainer = fresh.portainers.get(fresh.default_portainer)
        set_shared_portainer(self)
        for adapter in old:
            stop = getattr(adapter, "stop", None)
            if stop is not None:
                with contextlib.suppress(Exception):
                    await stop()
        log.info(
            "adapters.reloaded",
            portainers=sorted(self.portainers),
            proxmox=type(self.proxmox).__name__,
        )


@dataclass
class _Raw:
    power: PowerAdapter
    proxmox: ProxmoxAdapter
    pbs: PbsAdapter
    proxy: ProxyAdapter
    notifier: Notifier
    portainers: dict[str, PortainerAdapter]
    default_portainer: str


def build_notifier(settings: Settings) -> Notifier:
    """Channels from the environment: ntfy (NTFY_URL), e-mail (MAIL_SMTP_HOST +
    MAIL_FROM + MAIL_TO), webhook (NOTIFY_WEBHOOK_URL). ``NOTIFIER_ADAPTER`` =
    ``null`` (or DRY_RUN) logs only; ``auto`` uses every configured channel;
    ``ntfy`` / ``email`` / ``webhook`` restricts to that one."""
    kind = settings.notifier_adapter.strip().lower() or "null"
    if kind == "null" or settings.dry_run:
        return NullNotifier()
    channels: dict[str, Notifier] = {}
    if settings.ntfy_url.strip():
        channels["ntfy"] = NtfyNotifier(
            settings.ntfy_url.strip(), settings.ntfy_token.get_secret_value()
        )
    recipients = [r.strip() for r in settings.mail_to.split(",") if r.strip()]
    if settings.mail_smtp_host.strip() and recipients:
        channels["email"] = SmtpNotifier(
            host=settings.mail_smtp_host.strip(),
            port=settings.mail_smtp_port,
            username=settings.mail_smtp_username,
            password=settings.mail_smtp_password.get_secret_value(),
            sender=settings.mail_from or settings.mail_smtp_username,
            recipients=recipients,
            starttls=settings.mail_smtp_starttls,
        )
    if settings.notify_webhook_url.strip():
        channels["webhook"] = WebhookNotifier(
            settings.notify_webhook_url.strip(), settings.notify_webhook_token.get_secret_value()
        )
    # Phones and desktops that turned notifications on in the dashboard (PWA). Always
    # offered: with no device registered it just reports "no device".
    from orchestrator.notify.push import WebPushNotifier

    channels["push"] = WebPushNotifier(settings)
    if kind in {"ntfy", "email", "webhook", "push"}:
        channels = {k: v for k, v in channels.items() if k == kind}
    if not channels:
        log.warning(
            "adapters.notifier.no_channels",
            requested=kind,
            impact="Alerts are only logged and shown in the feed on /notifications.",
            fix="Set NTFY_URL, MAIL_SMTP_HOST + MAIL_TO, NOTIFY_WEBHOOK_URL, or turn on push on the Notifications page.",
        )
        return NullNotifier()
    return MultiNotifier(channels)


def _build_raw(settings: Settings) -> _Raw:

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

    portainers, default_portainer = _build_portainers(settings)

    notifier = build_notifier(settings)

    log.info(
        "adapters.selected",
        power=type(power).__name__,
        proxmox=type(proxmox).__name__,
        pbs=type(pbs).__name__,
        proxy=type(proxy).__name__,
        notifier=type(notifier).__name__,
        dry_run_flag=settings.dry_run,
    )
    return _Raw(power, proxmox, pbs, proxy, notifier, portainers, default_portainer)


def _build_portainers(settings: Settings) -> tuple[dict[str, PortainerAdapter], str]:
    """One adapter per enabled Portainer connection, and the id of the default one. With no
    Portainer connection at all there is still one (unconfigured, or a dry-run sample), called
    ``portainer``, so the rest of the app always has a Portainer to ask."""
    from orchestrator.connections import runtime

    def make(s: Settings) -> PortainerAdapter:
        if s.resolve_adapter(s.portainer_adapter) is AdapterMode.DRY_RUN:
            return DryRunPortainerAdapter(s)
        return LivePortainerAdapter(s)

    conns = runtime.instances_for(settings, "portainer")
    if not conns:
        adapter = make(settings)
        adapter.instance_id = "portainer"
        adapter.instance_name = "Portainer"
        return {"portainer": adapter}, "portainer"
    out: dict[str, PortainerAdapter] = {}
    for conn in conns:
        adapter = make(runtime.settings_for(settings, conn))
        adapter.instance_id = conn.id
        adapter.instance_name = conn.label
        out[conn.id] = adapter
    chosen = runtime.pick_default(conns)
    return out, (chosen.id if chosen is not None else next(iter(out)))


def build_adapters(settings: Settings | None = None) -> AdapterBundle:
    settings = settings or get_settings()
    raw = _build_raw(settings)
    bundle = AdapterBundle(
        power=Switchable(raw.power),  # type: ignore[arg-type]
        proxmox=Switchable(raw.proxmox),  # type: ignore[arg-type]
        pbs=Switchable(raw.pbs),  # type: ignore[arg-type]
        proxy=Switchable(raw.proxy),  # type: ignore[arg-type]
        notifier=Switchable(raw.notifier),  # type: ignore[arg-type]
        portainer=raw.portainers[raw.default_portainer],
        portainers=raw.portainers,
        default_portainer=raw.default_portainer,
    )
    set_shared_portainer(bundle)
    return bundle
