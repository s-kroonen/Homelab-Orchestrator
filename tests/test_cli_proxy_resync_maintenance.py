"""``orchestrator-cli proxy resync-maintenance`` — the backfill command for
hosts that predate ``ORCHESTRATOR_PUBLIC_URL``/``TRAEFIK_MANAGER_URL`` being
set, or that predate the maintenance-middleware feature entirely (see
docs/roadmap.md's "Quick fix" entry and docs/proxy_hosts.md's
"Maintenance-page fallback" section).

``sync_maintenance_middleware`` itself is exercised thoroughly against a
mocked Traefik Manager API in ``tests/test_traefik_manager_sync.py``; this
only has to prove the CLI command loops every linked ``ProxyHost`` and calls
it once per host with its current ``(hostname, service_slug)`` — so the
sync function is mocked here.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner
from sqlmodel import Session, select

from orchestrator import cli_config
from orchestrator import config as config_module
from orchestrator.cli import cli
from orchestrator.db.models import AuditEntry, Node, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.registry.traefik_manager_sync import MaintenanceSyncEvent


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAEFIK_MANAGER_URL", "http://tm.test:8080")
    monkeypatch.setenv("ORCHESTRATOR_PUBLIC_URL", "http://10.0.0.5:8000")
    config_module.reset_settings_cache()


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

    calls: list[dict[str, Any]] = []

    async def _fake_sync(settings: Any, **kwargs: Any) -> list[MaintenanceSyncEvent]:
        calls.append(kwargs)
        return [
            MaintenanceSyncEvent("middleware.injected", f"maintenance-{kwargs['service_slug']}", {})
        ]

    monkeypatch.setattr(cli_config, "sync_maintenance_middleware", _fake_sync)

    result = _run("proxy", "resync-maintenance")
    assert result.exit_code == 0, result.output
    assert "2 linked proxy host(s) processed." in result.output

    assert len(calls) == 2
    by_hostname = {c["hostname"]: c for c in calls}
    assert by_hostname["media.example.com"]["service_slug"] == "media"
    assert by_hostname["cloud.example.com"]["service_slug"] == "cloud"
    # Same-shape no-op resync: old == new.
    for c in calls:
        assert "old_hostname" not in c or c.get("old_hostname") is None
        assert "old_service_slug" not in c or c.get("old_service_slug") is None


def test_resync_maintenance_audits_each_event(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    svc = _service(session, "media")
    session.add(ProxyHost(hostname="media.example.com", service_id=svc.id))
    session.commit()

    async def _fake_sync(settings: Any, **kwargs: Any) -> list[MaintenanceSyncEvent]:
        return [
            MaintenanceSyncEvent("middleware.injected", "maintenance-media", {"route_id": "r1"})
        ]

    monkeypatch.setattr(cli_config, "sync_maintenance_middleware", _fake_sync)

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

    result = _run("proxy", "resync-maintenance")
    assert result.exit_code == 0, result.output
    assert "nothing to backfill" in result.output
