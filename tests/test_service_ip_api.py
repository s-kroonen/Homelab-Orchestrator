"""Service IPs end to end: the manual ``ip_address`` through the API, YAML and
CLI (one validator), ``POST /api/services/{slug}/ip/discover``, the bulk
``discover_ip`` action, ``orchestrator-cli ip``, the ping probe kind in the
API, the pages (Address block, dashboard IP, proxy form helper) and the
migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest, GuestNetwork
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.cli import cli
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, Node, Service, ServiceState
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.registry.ip_address import validate_ip_address
from orchestrator.registry.loader import (
    compute_registry_diff,
    reconcile_yaml_into_db,
    save_registry_to_yaml,
)
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

MAC = "bc:24:11:aa:bb:01"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    # The periodic job would race the tests' own discovery calls.
    monkeypatch.setenv("IP_DISCOVERY_ENABLED", "false")
    monkeypatch.setenv("HEALTH_SCAN_ENABLED", "false")
    from orchestrator.config import reset_settings_cache

    reset_settings_cache()
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def _seed(*services: tuple[str, int | None]) -> int:
    with Session(get_engine()) as s:
        node = Node(name="pve1", always_on=True, power_mgr_target="pve1")
        s.add(node)
        s.commit()
        s.refresh(node)
        for slug, vmid in services:
            s.add(
                Service(
                    slug=slug,
                    name=slug.title(),
                    node_id=node.id,
                    guest_kind=GuestKind.VM if vmid else GuestKind.NONE,
                    guest_id=vmid,
                )
            )
        s.commit()
        assert node.id is not None
        return node.id


def _proxmox(client: TestClient) -> DryRunProxmoxAdapter:
    px = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    assert isinstance(px, DryRunProxmoxAdapter)
    px.simulated_cluster = ClusterStatus(nodes=[ClusterNode("pve1", True, local=True)])
    return px


def _audit(action: str) -> list[AuditEntry]:
    with Session(get_engine()) as s:
        return list(s.exec(select(AuditEntry).where(AuditEntry.action == action)).all())


# ---------------------------------------------------------------------------
# the one validator
# ---------------------------------------------------------------------------


def test_validator_accepts_ipv4_ipv6_and_blank() -> None:
    assert validate_ip_address(" 10.0.0.5 ") == "10.0.0.5"
    assert validate_ip_address("[FD00::0005]") == "fd00::5"
    assert validate_ip_address("") == "" and validate_ip_address(None) == ""
    for bad in ("10.0.0.5/24", "nas.lan", "10.0.0.256", "10.0.0.5:80"):
        with pytest.raises(ValueError, match="not an IP address"):
            validate_ip_address(bad)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_put_manual_ip_validates_and_audits(client: TestClient) -> None:
    _seed(("media", 100))
    r = client.put("/api/registry/services/media", json={"ip_address": "nas.lan"})
    assert r.status_code == 400 and "not an IP address" in r.json()["detail"]
    r = client.put("/api/registry/services/media", json={"ip_address": " 10.0.0.5 "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ip_address"] == "10.0.0.5"
    assert (body["ip"]["effective_ip"], body["ip"]["effective_source"]) == ("10.0.0.5", "manual")
    changed = _audit("service.ip_changed")
    assert [(e.details["old"], e.details["new"], e.actor) for e in changed] == [
        ("", "10.0.0.5", "storm")
    ]
    # Clearing falls back to whatever was discovered.
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == "media")).one()
        s.add(ServiceState(service_id=svc.id, discovered_ip="10.0.0.40", ip_source="arp"))  # type: ignore[arg-type]
        s.commit()
    r = client.put("/api/registry/services/media", json={"ip_address": ""})
    assert r.json()["ip"]["effective_ip"] == "10.0.0.40"
    assert r.json()["ip"]["effective_source"] == "arp"


def test_create_service_with_ip(client: TestClient) -> None:
    node_id = _seed()
    r = client.post(
        "/api/registry/services",
        json={"slug": "nas", "name": "NAS", "node_id": node_id, "ip_address": "10.0.0.9"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["ip_address"] == "10.0.0.9"
    r = client.post(
        "/api/registry/services",
        json={"slug": "nas2", "name": "NAS", "node_id": node_id, "ip_address": "x"},
    )
    assert r.status_code == 400


def test_discover_endpoint_finds_and_stores(client: TestClient) -> None:
    _seed(("media", 100))
    px = _proxmox(client)
    px.simulated_guests = [Guest("pve1", 100, GuestKind.VM, "media", "running")]
    px.simulated_networks[100] = GuestNetwork(macs=[MAC], ips=["10.0.0.21"], source="guest_agent")
    r = client.post("/api/services/media/ip/discover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["ip"], body["source"], body["changed"]) == (
        "found",
        "10.0.0.21",
        "guest_agent",
        True,
    )
    assert body["effective_ip"] == "10.0.0.21" and body["mac_addresses"] == [MAC]
    got = client.get("/api/services/media/ip").json()
    assert got["effective_source_label"] == "guest agent"
    listed = {s["slug"]: s for s in client.get("/api/registry/services").json()}
    assert listed["media"]["effective_ip"] == "10.0.0.21"
    # Runtime state only: the registry is not dirty because of it.
    with Session(get_engine()) as s:
        save_registry_to_yaml(s, get_settings().services_yaml_path)
        s.commit()
    px.simulated_networks[100] = GuestNetwork(macs=[MAC], ips=["10.0.0.22"], source="guest_agent")
    client.post("/api/services/media/ip/discover")
    assert client.get("/api/registry/status").json()["dirty"] is False
    saved = yaml.safe_load(get_settings().services_yaml_path.read_text(encoding="utf-8"))
    assert saved["services"][0]["ip_address"] == ""  # discovered IPs never reach YAML


def test_discover_endpoint_skips_a_stopped_guest(client: TestClient) -> None:
    _seed(("media", 100))
    px = _proxmox(client)
    px.simulated_guests = [Guest("pve1", 100, GuestKind.VM, "media", "stopped")]
    r = client.post("/api/services/media/ip/discover")
    assert r.status_code == 200 and r.json()["status"] == "skipped"
    assert px.guest_network_calls == []
    assert client.post("/api/services/nope/ip/discover").status_code == 404


def test_bulk_discover_ip(client: TestClient) -> None:
    _seed(("media", 100), ("off", 101), ("bare", None))
    px = _proxmox(client)
    px.simulated_guests = [
        Guest("pve1", 100, GuestKind.VM, "media", "running"),
        Guest("pve1", 101, GuestKind.VM, "off", "stopped"),
    ]
    px.simulated_networks[100] = GuestNetwork(macs=[MAC], ips=["10.0.0.21"], source="guest_agent")
    r = client.post(
        "/api/bulk/services", json={"slugs": ["media", "off", "bare"], "action": "discover_ip"}
    )
    assert r.status_code == 200, r.text
    results = {x["id"]: x for x in r.json()["results"]}
    assert results["media"]["ok"] and "10.0.0.21" in results["media"]["message"]
    assert results["off"]["ok"] and "stopped" in results["off"]["message"]
    assert results["bare"]["ok"] and "manually" in results["bare"]["message"]
    assert [e.target for e in _audit("service.ip_changed")] == ["service:media"]


def test_ping_probe_via_api(client: TestClient) -> None:
    _seed(("media", 100))
    ok = client.post(
        "/api/registry/services/media/probes",
        json={"name": "ping", "kind": "ping", "required": False, "config": {"host": ""}},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["timeout_s"] == 10
    bad = client.post(
        "/api/registry/services/media/probes",
        json={"name": "p2", "kind": "ping", "config": {"transport": {"type": "ssh"}}},
    )
    assert bad.status_code == 400 and "transport.host" in bad.json()["detail"]
    bad = client.post(
        "/api/registry/services/media/probes",
        json={"name": "p3", "kind": "ping", "config": {"count": 99}},
    )
    assert bad.status_code == 400
    # No IP yet: testing it says so instead of pinging nothing.
    result = client.post(f"/api/registry/probes/{ok.json()['id']}/test").json()
    assert result["state"] == "unknown" and "no IP yet" in result["message"]


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def test_service_page_renders_the_address_block(client: TestClient) -> None:
    _seed(("media", 100))
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == "media")).one()
        s.add(
            ServiceState(
                service_id=svc.id,  # type: ignore[arg-type]
                discovered_ip="10.0.0.21",
                ip_source="arp",
                mac_addresses=[MAC],
            )
        )
        s.commit()
    html = client.get("/services/media").text
    assert "10.0.0.21" in html and MAC in html
    js = client.get("/static/service.js").text
    assert '"/ip/discover"' in js and "function addPing" in js  # find now, one-click ping
    assert 'key: "ip_address", label: "Address by hand"' in js
    assert 'show: is("ping")' in js  # the probe drawer knows the kind


def test_dashboard_shows_the_ip_under_the_name(client: TestClient) -> None:
    _seed(("media", 100))
    client.put("/api/registry/services/media", json={"ip_address": "10.0.0.5"})
    html = client.get("/").text
    assert '<span class="svc-ip" title="IP (manual)">10.0.0.5</span>' in html
    assert 'class="bulk-cb"' in html and 'action: "discover_ip"' in html


def test_proxy_form_offers_use_service_ip(client: TestClient) -> None:
    _seed(("media", 100))
    client.put("/api/registry/services/media", json={"ip_address": "10.0.0.5"})
    html = client.get("/proxy-hosts?new=1").text
    assert '"ip": "10.0.0.5"' in html  # the drawer's picker knows each service's address
    js = client.get("/static/objects.js").text
    assert "Its service's address is" in js and '"Use it"' in js


# ---------------------------------------------------------------------------
# YAML round trip
# ---------------------------------------------------------------------------

REGISTRY = """\
version: 1
nodes:
  - name: pve1
    always_on: true
backup_policies: []
services:
  - slug: media
    name: Media
    node: pve1
    guest_kind: vm
    guest_id: 100
    ip_address: 10.0.0.5
  - slug: photos
    name: Photos
    node: pve1
    guest_kind: vm
    guest_id: 101
"""


def test_yaml_round_trip_and_discovery_never_dirties(tmp_path: Path) -> None:
    path = get_settings().services_yaml_path
    path.write_text(REGISTRY, encoding="utf-8")
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
        media = s.exec(select(Service).where(Service.slug == "media")).one()
        photos = s.exec(select(Service).where(Service.slug == "photos")).one()
        assert media.ip_address == "10.0.0.5" and photos.ip_address == ""
        assert compute_registry_diff(s).dirty is False
        s.add(ServiceState(service_id=photos.id, discovered_ip="10.0.0.9", ip_source="arp"))  # type: ignore[arg-type]
        s.commit()
        assert compute_registry_diff(s).dirty is False
        save_registry_to_yaml(s, path)
        s.commit()
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    by_slug = {svc["slug"]: svc for svc in saved["services"]}
    assert by_slug["media"]["ip_address"] == "10.0.0.5"
    assert by_slug["photos"]["ip_address"] == ""  # discovered IP never written


def test_yaml_with_a_bad_ip_is_refused(tmp_path: Path) -> None:
    path = get_settings().services_yaml_path
    path.write_text(REGISTRY.replace("10.0.0.5", "10.0.0.5/24"), encoding="utf-8")
    session_module.create_all_for_tests()
    with Session(get_engine()) as s, pytest.raises(ValueError, match="not an IP address"):
        reconcile_yaml_into_db(s, path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_service_update_ip(tmp_path: Path) -> None:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    run = CliRunner().invoke
    r = run(cli, ["service", "update", "photos", "--ip", "10.0.0.6", "--file", str(path)])
    assert r.exit_code == 0, r.output
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert next(s for s in doc["services"] if s["slug"] == "photos")["ip_address"] == "10.0.0.6"
    r = run(cli, ["service", "update", "photos", "--ip", "nope", "--file", str(path)])
    assert r.exit_code != 0 and "not an IP address" in r.output
    r = run(cli, ["service", "update", "photos", "--ip", "", "--file", str(path)])
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert next(s for s in doc["services"] if s["slug"] == "photos")["ip_address"] == ""


def test_cli_ip_discover_and_show(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings().services_yaml_path.write_text(REGISTRY, encoding="utf-8")
    session_module.create_all_for_tests()
    px = DryRunProxmoxAdapter(
        simulated_guests=[
            Guest("pve1", 100, GuestKind.VM, "media", "running"),
            Guest("pve1", 101, GuestKind.VM, "photos", "running"),
        ]
    )
    px.simulated_cluster = ClusterStatus(nodes=[ClusterNode("pve1", True, local=True)])
    px.simulated_networks[101] = GuestNetwork(macs=[MAC], ips=["10.0.0.9"], source="guest_agent")

    import orchestrator.cli_ip as cli_ip
    from orchestrator.adapters.factory import build_adapters

    def fake_build(settings: Any) -> Any:
        bundle = build_adapters(settings)
        bundle.proxmox = px
        return bundle

    monkeypatch.setattr(cli_ip, "build_adapters", fake_build)
    run = CliRunner().invoke
    assert run(cli, ["ip", "discover"]).exit_code != 0  # needs SLUGs or --all
    r = run(cli, ["ip", "discover", "--all"])
    assert r.exit_code == 0, r.output
    assert "10.0.0.9" in r.output and "FOUND" in r.output
    r = run(cli, ["ip", "show"])
    assert r.exit_code == 0, r.output
    lines = {line.split()[0]: line for line in r.output.splitlines()[1:] if line.strip()}
    assert "10.0.0.5" in lines["media"] and "manual" in lines["media"]
    assert "10.0.0.9" in lines["photos"] and "guest agent" in lines["photos"] and MAC in r.output
    assert run(cli, ["ip", "discover", "ghost"]).exit_code == 1


def test_cli_probe_builder_knows_ping(tmp_path: Path) -> None:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    r = CliRunner().invoke(
        cli,
        ["probe", "add", "photos", "--kind", "ping", "--non-interactive", "--file", str(path)],
    )
    assert r.exit_code == 0, r.output
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    probe = next(s for s in doc["services"] if s["slug"] == "photos")["probes"][0]
    assert probe["kind"] == "ping" and probe["config"] == {"count": 1}


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_0014_adds_ip_columns_and_drops_them_on_downgrade() -> None:
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "0013_proxy_host_enabled")
    engine = sa.create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO node (name, always_on, power_mgr_target, notes, wake_on_hit_enabled, created_at, updated_at) VALUES ('n', 1, '', '', 1, '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO service (slug, name, description, guest_kind, enabled, backup_excluded, backup_excluded_reason, wake_on_hit_enabled, \"group\", depends_on, backup_requires, node_id, created_at, updated_at) VALUES ('s', 'S', '', 'NONE', 1, 0, '', 1, '', '[]', '[]', 1, '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO service_state (service_id, last_verdict, last_probe_results, notes) VALUES (1, 'UNKNOWN', '[]', '')"
            )
        )
    command.upgrade(cfg, "0014_service_ip")
    with engine.connect() as conn:
        insp = sa.inspect(conn)
        assert "ip_address" in {c["name"] for c in insp.get_columns("service")}
        state_cols = {c["name"] for c in insp.get_columns("service_state")}
        assert {
            "discovered_ip",
            "ip_source",
            "mac_addresses",
            "ip_discovered_at",
            "ip_discovery_error",
        } <= state_cols
        assert conn.execute(sa.text("SELECT ip_address FROM service")).scalar_one() == ""
        row = conn.execute(sa.text("SELECT discovered_ip, mac_addresses FROM service_state")).one()
        assert row[0] == "" and row[1] == "[]"
    command.downgrade(cfg, "0013_proxy_host_enabled")
    with engine.connect() as conn:
        insp = sa.inspect(conn)
        assert "ip_address" not in {c["name"] for c in insp.get_columns("service")}
        assert "discovered_ip" not in {c["name"] for c in insp.get_columns("service_state")}
    engine.dispose()
