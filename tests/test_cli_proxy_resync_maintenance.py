"""``orchestrator-cli proxy resync-maintenance`` — the backfill command for
hosts that predate ``ORCHESTRATOR_PUBLIC_URL``/``TRAEFIK_MANAGER_URL`` being
set, or that predate the maintenance-middleware feature entirely (see
docs/roadmap.md's "Quick fix" entry and docs/proxy_hosts.md's
"Maintenance-page fallback" section).

``TraefikManagerProxyAdapter.ensure_maintenance_fallback`` itself is
exercised thoroughly against a mocked Traefik Manager API in
``tests/test_proxy_adapter_traefik_manager.py``; this only has to prove the
CLI command loops every linked ``ProxyHost`` and calls it once per host with
its current ``(hostname, service_slug)`` — so ``build_adapters`` is mocked
here to hand back a fake ``ProxyAdapter``.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner
from sqlmodel import Session, select

from orchestrator import cli_config
from orchestrator import config as config_module
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.proxy.base import SyncEvent
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.cli import cli
from orchestrator.db.models import AuditEntry, Node, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAEFIK_MANAGER_URL", "http://tm.test:8080")
    monkeypatch.setenv("ORCHESTRATOR_PUBLIC_URL", "http://10.0.0.5:8000")
    config_module.reset_settings_cache()


class _FakeProxyAdapter(DryRunProxyAdapter):
    """Records every ``ensure_maintenance_fallback`` call and returns a
    scripted event list instead of touching the dry-run defaults."""

    def __init__(self, events: list[SyncEvent] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._events = events if events is not None else []

    async def ensure_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        self.calls.append({"hostname": hostname, "service_slug": service_slug})
        if self._events:
            return [
                SyncEvent(e.action, f"maintenance-{service_slug}", e.details) for e in self._events
            ]
        return []


def _patch_proxy_adapter(monkeypatch: pytest.MonkeyPatch, adapter: _FakeProxyAdapter) -> None:
    def _fake_build_adapters(settings: Any = None) -> AdapterBundle:
        from orchestrator.adapters.notifier.null import NullNotifier
        from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
        from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
        from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter

        return AdapterBundle(
            power=DryRunPowerAdapter(),
            proxmox=DryRunProxmoxAdapter(),
            pbs=DryRunPbsAdapter(),
            proxy=adapter,
            notifier=NullNotifier(),
        )

    monkeypatch.setattr(cli_config, "build_adapters", _fake_build_adapters)


def _service(session: Session, slug: str) -> Service:
    node = Node(name=f"node-{slug}", always_on=True)
    session.add(node)
    session.commit()
    session.refresh(node)
    svc = Service(
        slug=slug, name=slug.title(), node_id=node.id, guest_kind=GuestKind.VM, guest_id=1
    )
    session.add(svc)
    session.commit()
    session.refresh(svc)
    return svc


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args))


def test_resync_maintenance_calls_sync_once_per_linked_host(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    svc_a = _service(session, "media")
    svc_b = _service(session, "cloud")
    session.add(ProxyHost(hostname="media.example.com", service_id=svc_a.id))
    session.add(ProxyHost(hostname="cloud.example.com", service_id=svc_b.id))
    # Unlinked — must be skipped.
    session.add(ProxyHost(hostname="unlinked.example.com", service_id=None))
    session.commit()

    adapter = _FakeProxyAdapter([SyncEvent("middleware.injected", "unused", {})])
    _patch_proxy_adapter(monkeypatch, adapter)

    result = _run("proxy", "resync-maintenance")
    assert result.exit_code == 0, result.output
    assert "2 linked proxy host(s) processed." in result.output

    assert len(adapter.calls) == 2
    by_hostname = {c["hostname"]: c for c in adapter.calls}
    assert by_hostname["media.example.com"]["service_slug"] == "media"
    assert by_hostname["cloud.example.com"]["service_slug"] == "cloud"


def test_resync_maintenance_audits_each_event(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    svc = _service(session, "media")
    session.add(ProxyHost(hostname="media.example.com", service_id=svc.id))
    session.commit()

    adapter = _FakeProxyAdapter(
        [SyncEvent("middleware.injected", "maintenance-media", {"route_id": "r1"})]
    )
    _patch_proxy_adapter(monkeypatch, adapter)

    result = _run("proxy", "resync-maintenance")
    assert result.exit_code == 0, result.output

    with Session(get_engine()) as s:
        entries = s.exec(select(AuditEntry).where(AuditEntry.action == "middleware.injected")).all()
    assert len(entries) == 1
    assert entries[0].actor == "cli"
    assert entries[0].target == "traefik_manager:maintenance-media"


def test_resync_maintenance_no_op_without_settings(session: Session) -> None:
    # TRAEFIK_MANAGER_URL / ORCHESTRATOR_PUBLIC_URL are unset by the autouse
    # _isolated_settings fixture's bare environment.
    result = _run("proxy", "resync-maintenance")
    assert result.exit_code == 0, result.output
    assert "must both be set" in result.output


def test_resync_maintenance_no_linked_hosts(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    session.add(ProxyHost(hostname="unlinked.example.com", service_id=None))
    session.commit()

    adapter = _FakeProxyAdapter()
    _patch_proxy_adapter(monkeypatch, adapter)

    result = _run("proxy", "resync-maintenance")
    assert result.exit_code == 0, result.output
    assert "nothing to backfill" in result.output
