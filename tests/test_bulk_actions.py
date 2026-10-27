"""Bulk actions: ``POST /api/bulk/{services,proxy-hosts,nodes}``, the guest
power endpoint, the ``orchestrator-cli bulk`` commands and the page markup.

The bulk endpoints reuse the single-item code paths, so these tests check what
is specific to doing many at once: per-item results with mixed success and
failure, the single-item validation still applying per item, one audit entry
per item (same action names as the single-item endpoints) plus one
``bulk.<resource>`` summary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.adapters.proxmox.base import Guest
from orchestrator.cli import cli
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, BackupPolicy, Node, Probe, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, ProbeKind, RouterProvider
from orchestrator.web.app import create_app
from tests.test_proxy_entrypoints_api import ROUTE_REF_KEY, _body, _client
from tests.test_proxy_host_enabled import ToggleTM
from tests.test_proxy_hosts import _login_as_first_admin


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def _audit(action: str) -> list[AuditEntry]:
    with Session(get_engine()) as s:
        return list(s.exec(select(AuditEntry).where(AuditEntry.action == action)).all())


def _seed_services(*slugs: str, probes_required: bool = False) -> int:
    with Session(get_engine()) as s:
        node = Node(name="eve1", always_on=True, power_mgr_target="eve1")
        s.add(node)
        s.commit()
        s.refresh(node)
        for i, slug in enumerate(slugs):
            svc = Service(
                slug=slug,
                name=slug.title(),
                node_id=node.id,
                guest_kind=GuestKind.VM,
                guest_id=100 + i,
            )
            s.add(svc)
            s.commit()
            s.refresh(svc)
            for name in ("http", "tcp"):
                s.add(
                    Probe(
                        service_id=svc.id,  # type: ignore[arg-type]
                        name=name,
                        kind=ProbeKind.TCP,
                        required=probes_required,
                        config={"host": "127.0.0.1", "port": 1},
                    )
                )
            s.commit()
        assert node.id is not None
        return node.id


def _service(slug: str) -> Service:
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == slug)).one()
        s.expunge(svc)
        return svc


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


def test_bulk_disable_services_reports_each_item(client: TestClient) -> None:
    _seed_services("media", "photos")
    r = client.post(
        "/api/bulk/services", json={"slugs": ["media", "nope", "photos"], "action": "disable"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(x["id"], x["ok"]) for x in body["results"]] == [
        ("media", True),
        ("nope", False),
        ("photos", True),
    ]
    assert "nope" in body["results"][1]["message"]
    assert (body["ok_count"], body["failed_count"]) == (2, 1)
    assert _service("media").enabled is False and _service("photos").enabled is False
    # One entry per item, same action as PUT /api/registry/services/{slug} ...
    assert sorted(e.target for e in _audit("service.updated")) == [
        "service:media",
        "service:photos",
    ]
    # ... plus one summary.
    (summary,) = _audit("bulk.services")
    assert summary.details["action"] == "disable"
    assert summary.details["ok"] == ["media", "photos"]
    assert summary.details["failed"][0]["id"] == "nope"

    r = client.post("/api/bulk/services", json={"ids": ["media", "photos"], "action": "enable"})
    assert r.json()["ok_count"] == 2 and _service("media").enabled is True


def test_bulk_probes_required_goes_through_the_probe_update(client: TestClient) -> None:
    _seed_services("media", "photos")
    r = client.post(
        "/api/bulk/services", json={"ids": ["media", "photos"], "action": "probes_required"}
    )
    assert r.json()["ok_count"] == 2, r.text
    assert r.json()["results"][0]["message"] == "2 of 2 probe(s) now required"
    with Session(get_engine()) as s:
        assert all(p.required for p in s.exec(select(Probe)).all())
    assert len(_audit("probe.updated")) == 4
    r = client.post("/api/bulk/services", json={"ids": ["media"], "action": "probes_required"})
    assert r.json()["results"][0]["message"] == "all 2 probe(s) already required"
    client.post("/api/bulk/services", json={"ids": ["media"], "action": "probes_optional"})
    with Session(get_engine()) as s:
        media = _service("media")
        assert not any(
            p.required for p in s.exec(select(Probe).where(Probe.service_id == media.id)).all()
        )


def test_bulk_update_group_and_policy_reuses_validation(client: TestClient) -> None:
    _seed_services("media", "photos")
    with Session(get_engine()) as s:
        pol = BackupPolicy(name="nightly")
        s.add(pol)
        s.commit()
        s.refresh(pol)
        pol_id = pol.id
    r = client.post(
        "/api/bulk/services",
        json={"ids": ["media", "photos"], "action": "update", "params": {"group": "Media"}},
    )
    assert r.json()["ok_count"] == 2
    assert _service("photos").group == "Media"
    r = client.post(
        "/api/bulk/services",
        json={"ids": ["media"], "action": "update", "params": {"backup_policy_id": pol_id}},
    )
    assert r.json()["ok_count"] == 1 and _service("media").backup_policy_id == pol_id
    # The single-item check (policy must exist) fails the item, not the request.
    r = client.post(
        "/api/bulk/services",
        json={"ids": ["media"], "action": "update", "params": {"backup_policy_id": 999}},
    )
    assert r.status_code == 200 and r.json()["failed_count"] == 1
    # Only the simple fields are bulk-editable.
    r = client.post(
        "/api/bulk/services",
        json={"ids": ["media"], "action": "update", "params": {"node_id": 1}},
    )
    assert r.status_code == 400


def test_bulk_rejects_unknown_action_and_empty_selection(client: TestClient) -> None:
    _seed_services("media")
    assert (
        client.post("/api/bulk/services", json={"ids": ["media"], "action": "explode"}).status_code
        == 400
    )
    assert (
        client.post("/api/bulk/services", json={"ids": [], "action": "enable"}).status_code == 400
    )
    assert (
        client.post("/api/bulk/nodes", json={"ids": ["x"], "action": "restart"}).status_code == 400
    )


def test_bulk_wake_starts_a_run_per_service(client: TestClient) -> None:
    _seed_services("media", "photos")
    r = client.post("/api/bulk/services", json={"ids": ["media", "photos"], "action": "wake"})
    assert r.status_code == 200, r.text
    assert r.json()["ok_count"] == 2
    assert all("wake run #" in x["message"] for x in r.json()["results"])


def test_bulk_requires_admin() -> None:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as anon:
        for path in ("/api/bulk/services", "/api/bulk/proxy-hosts", "/api/bulk/nodes"):
            assert anon.post(path, json={"ids": ["a"], "action": "enable"}).status_code in (
                401,
                403,
            )


# ---------------------------------------------------------------------------
# proxy hosts
# ---------------------------------------------------------------------------


def test_bulk_disable_proxy_hosts_is_db_only_and_the_push_reports_the_failing_one(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.push_helpers import actions, failures, push

    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    a = client.post("/api/proxy-hosts", json=_body(service, hostname="a.example.com")).json()
    b = client.post("/api/proxy-hosts", json=_body(service, hostname="b.example.com")).json()
    push(client)
    tm.fail_toggle.add(b["extra"][ROUTE_REF_KEY])

    r = client.post("/api/bulk/proxy-hosts", json={"ids": [a["id"], b["id"]], "action": "disable"})
    assert r.status_code == 200, r.text
    assert [x["ok"] for x in r.json()["results"]] == [True, True]
    assert tm.toggles == []  # bulk actions only touch the DB
    with Session(get_engine()) as s:
        assert s.get(ProxyHost, a["id"]).enabled is False  # type: ignore[union-attr]
        assert s.get(ProxyHost, b["id"]).enabled is False  # type: ignore[union-attr]
    assert len(_audit("proxy_host.disabled")) == 2

    result = push(client)
    assert actions(result) == [("a.example.com", "disable")]  # one host failing did not stop a
    (fail,) = failures(result)
    assert fail["hostname"] == "b.example.com" and "could not disable" in fail["message"]


def test_bulk_proxy_host_wake_on_hit_and_delete(client: TestClient, service: Service) -> None:
    ids = [
        client.post(
            "/api/proxy-hosts",
            json={
                "hostname": f"{n}.example.com",
                "service_id": service.id,
                "router_provider": "npm",
            },
        ).json()["id"]
        for n in ("a", "b")
    ]
    r = client.post("/api/bulk/proxy-hosts", json={"ids": ids, "action": "wake_on_hit_on"})
    assert r.json()["ok_count"] == 2
    assert all(h["wake_on_hit"] for h in client.get("/api/proxy-hosts").json())
    assert len(_audit("proxy_host.updated")) == 2

    r = client.post("/api/bulk/proxy-hosts", json={"ids": [*ids, 999], "action": "delete"})
    assert [x["ok"] for x in r.json()["results"]] == [True, True, False]
    assert client.get("/api/proxy-hosts").json() == []
    assert len(_audit("proxy_host.deleted")) == 2


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


def test_bulk_nodes_wake_and_wake_on_hit(client: TestClient) -> None:
    _seed_services("media")
    r = client.post("/api/bulk/nodes", json={"names": ["eve1", "ghost"], "action": "wake"})
    assert [x["ok"] for x in r.json()["results"]] == [True, False]
    (entry,) = _audit("node.power_action")
    assert entry.details == {"action": "wake", "force": False}

    r = client.post(
        "/api/bulk/nodes",
        json={"names": ["eve1"], "action": "power_off", "params": {"force": True}},
    )
    assert r.json()["results"][0]["message"] == "power_off sent (force)"
    assert _audit("node.power_action")[-1].details == {"action": "power_off", "force": True}

    r = client.post("/api/bulk/nodes", json={"names": ["eve1"], "action": "wake_on_hit_off"})
    assert r.json()["ok_count"] == 1
    assert client.get("/api/registry/nodes").json()[0]["wake_on_hit_enabled"] is False
    assert len(_audit("bulk.nodes")) == 3


# ---------------------------------------------------------------------------
# guests
# ---------------------------------------------------------------------------


def _fake_guests(client: TestClient) -> None:
    async def fake_list_guests(node: str | None = None) -> list[Guest]:
        return [
            Guest(node="eve1", vmid=100, kind=GuestKind.VM, name="media", status="stopped"),
            Guest(node="eve1", vmid=900, kind=GuestKind.CT, name="scratch", status="running"),
        ]

    client.app.state.adapters.proxmox.list_guests = fake_list_guests  # type: ignore[attr-defined]


def test_guest_power_for_an_unregistered_guest(client: TestClient) -> None:
    _seed_services("media")  # media is vm 100 on eve1
    _fake_guests(client)
    listed = client.get("/api/infra/proxmox/guests").json()
    assert [g["service_slug"] for g in listed] == ["media", None]

    r = client.post("/api/infra/guests/eve1/900/power", json={"action": "stop"})
    assert r.status_code == 200, r.text
    (entry,) = _audit("infra.guest_power_action")
    assert entry.target == "guest:eve1/900"
    assert entry.details["guest_kind"] == "ct" and entry.details["action"] == "stop"

    assert (
        client.post("/api/infra/guests/eve1/555/power", json={"action": "start"}).status_code == 404
    )
    assert (
        client.post("/api/infra/guests/eve1/900/power", json={"action": "reboot"}).status_code
        == 422
    )


def test_guest_power_for_a_registered_guest_uses_the_service_path(client: TestClient) -> None:
    _seed_services("media")
    _fake_guests(client)
    r = client.post("/api/infra/guests/eve1/100/power", json={"action": "start"})
    assert r.status_code == 200, r.text
    assert r.json()["service"] == "media"
    assert len(_audit("service.guest_power_action")) == 1
    assert _audit("infra.guest_power_action") == []


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def test_pages_render_the_selection_column_and_bar(client: TestClient) -> None:
    _seed_services("media")
    page = client.get("/").text
    assert '<script src="/static/bulk.js"></script>' in page
    assert 'class="bulk-cb" data-bulk-id="media"' in page
    assert '<th class="bulk-col"></th>' in page
    assert (
        "Bulk.attach({" in page and '"/api/bulk/services"' in page and '"/api/bulk/nodes"' in page
    )
    assert "/static/proxy_hosts.js?v=" in client.get("/proxy-hosts").text
    ph = client.get("/static/proxy_hosts.js").text
    assert 'bulk("proxy-hosts"' in ph and "bulk: (rows)" in ph  # the shared table's bulk bar
    assert '"/api/bulk/" + resource' in client.get("/static/objects.js").text
    guests = client.get("/infra/guests").text
    assert "/static/bulk.js" in guests and "/api/infra/guests/" in guests
    js = client.get("/static/bulk.js")
    assert js.status_code == 200 and "Select all visible" in js.text
    assert client.get("/static/bulk.css").status_code == 200


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SAMPLE = """\
version: 1
nodes:
  - name: eve1
    always_on: true
    power_mgr_target: eve1
    notes: ""
backup_policies: []
services:
  - slug: media
    name: Media
    group: home
    node: eve1
    guest_kind: vm
    guest_id: 111
    enabled: true
    probes:
      - name: web
        kind: tcp
        required: false
        config: {host: 127.0.0.1, port: 80}
    proxy_hosts:
      - hostname: media.example.com
        upstream: http://10.0.0.5:80
        router_provider: npm
  - slug: photos
    name: Photos
    group: home
    node: eve1
    guest_kind: vm
    guest_id: 112
    enabled: true
    probes: []
    proxy_hosts: []
  - slug: dns
    name: DNS
    node: eve1
    guest_kind: vm
    guest_id: 113
    enabled: true
    probes: []
    proxy_hosts: []
"""


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def _svc(registry: Path, slug: str) -> dict[str, Any]:
    doc = yaml.safe_load(registry.read_text(encoding="utf-8"))
    return next(s for s in doc["services"] if s["slug"] == slug)


def test_cli_bulk_services_disable_by_group_and_slug(registry: Path) -> None:
    result = CliRunner().invoke(cli, ["bulk", "services", "disable", "--group", "home"])
    assert result.exit_code == 0, result.output
    assert "ok      media" in result.output and "2 ok, 0 failed" in result.output
    assert _svc(registry, "media")["enabled"] is False
    assert _svc(registry, "photos")["enabled"] is False
    assert _svc(registry, "dns")["enabled"] is True

    result = CliRunner().invoke(cli, ["bulk", "services", "enable", "--slug", "media,nope"])
    assert result.exit_code == 1  # one item failed
    assert "FAILED  nope" in result.output
    assert _svc(registry, "media")["enabled"] is True


def test_cli_bulk_services_probes_required_and_dry_run(registry: Path) -> None:
    before = registry.read_text(encoding="utf-8")
    result = CliRunner().invoke(cli, ["bulk", "services", "probes-required", "--all", "--dry-run"])
    assert result.exit_code == 0 and "Would probes-required: media, photos, dns" in result.output
    assert registry.read_text(encoding="utf-8") == before

    result = CliRunner().invoke(cli, ["bulk", "services", "probes-required", "--all"])
    assert result.exit_code == 0, result.output
    assert _svc(registry, "media")["probes"][0]["required"] is True
    assert "no probes" in result.output


def test_cli_bulk_services_needs_a_selection(registry: Path) -> None:
    result = CliRunner().invoke(cli, ["bulk", "services", "disable"])
    assert result.exit_code == 2 and "nothing selected" in result.output


def test_cli_bulk_proxy_hosts_disable(registry: Path) -> None:
    from orchestrator.registry.loader import reconcile_yaml_into_db

    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, registry)
        s.commit()
    result = CliRunner().invoke(cli, ["bulk", "proxy-hosts", "disable", "--service", "media"])
    assert result.exit_code == 0, result.output
    assert "1 ok, 0 failed" in result.output
    with Session(get_engine()) as s:
        (ph,) = s.exec(select(ProxyHost)).all()
        assert ph.enabled is False and ph.router_provider is RouterProvider.NPM
    # Mirrored into services.yaml so a reload agrees.
    assert _svc(registry, "media")["proxy_hosts"][0]["enabled"] is False
    assert len(_audit("proxy_host.disabled")) == 1 and len(_audit("bulk.proxy_hosts")) == 1

    result = CliRunner().invoke(
        cli, ["bulk", "proxy-hosts", "enable", "--hostname", "media.example.com"]
    )
    assert result.exit_code == 0, result.output
    assert "enabled" not in _svc(registry, "media")["proxy_hosts"][0]  # default, omitted


def test_bulk_services_wake_on_hit_and_trigger(client: TestClient) -> None:
    _seed_services("media", "photos")
    r = client.post(
        "/api/bulk/services", json={"action": "wake_on_hit_off", "slugs": ["media", "photos"]}
    )
    assert r.status_code == 200 and r.json()["ok_count"] == 2
    assert _service("media").wake_on_hit_enabled is False
    client.post("/api/bulk/services", json={"action": "wake_on_hit_on", "slugs": ["media"]})
    assert _service("media").wake_on_hit_enabled is True

    r = client.post(
        "/api/bulk/services",
        json={
            "action": "update",
            "slugs": ["media", "photos"],
            "params": {"wake_trigger": "button"},
        },
    )
    assert r.json()["ok_count"] == 2
    assert _service("media").wake_trigger.value == "button"
    assert client.get("/api/registry/services/media").json()["wake_trigger"] == "button"

    bad = client.post(
        "/api/bulk/services",
        json={"action": "update", "slugs": ["media"], "params": {"wake_trigger": "shout"}},
    )
    assert bad.json()["failed_count"] == 1
    assert _service("media").wake_trigger.value == "button"


def test_cli_bulk_services_wake_trigger_and_wake_on_hit(registry: Path) -> None:
    result = CliRunner().invoke(cli, ["bulk", "services", "wake-trigger-button", "--group", "home"])
    assert result.exit_code == 0, result.output
    assert _svc(registry, "media")["wake_trigger"] == "button"
    assert "wake_trigger" not in _svc(registry, "dns")
    result = CliRunner().invoke(cli, ["bulk", "services", "wake-on-hit-off", "--all"])
    assert result.exit_code == 0, result.output
    assert _svc(registry, "dns")["wake_on_hit_enabled"] is False


def test_cli_service_update_sets_wake_trigger(registry: Path) -> None:
    result = CliRunner().invoke(
        cli, ["service", "update", "media", "--wake-trigger", "button", "--no-wake-on-hit"]
    )
    assert result.exit_code == 0, result.output
    svc = _svc(registry, "media")
    assert svc["wake_trigger"] == "button" and svc["wake_on_hit_enabled"] is False
