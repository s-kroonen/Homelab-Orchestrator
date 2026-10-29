"""The service tree: sub-services under a root service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest
from orchestrator.db import session as session_module
from orchestrator.db.models import Node, Probe, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, HealthState
from orchestrator.registry.loader import (
    read_yaml_file,
    reconcile_yaml_into_db,
    save_registry_to_yaml,
    validate_references,
)
from orchestrator.registry.schema import RegistryFile
from orchestrator.registry.tree import TreeIndex, effective_guest, is_served
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

REGISTRY = """
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: portainer
    name: Portainer
    node: pve1
    guest_kind: vm
    guest_id: 101
    probes:
      - {name: api, kind: tcp, config: {host: 10.0.0.5, port: 9443}}
  - slug: nginx
    name: nginx container
    parent: portainer
    backup_gate: required
    controller: {type: portainer, endpoint: 2, container: nginx}
    recovery: {enabled: true, max_attempts: 2}
    proxy_hosts:
      - {hostname: nginx.example.com, upstream: "http://10.0.0.5:8080"}
"""


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    return path


def _services() -> dict[str, Service]:
    with Session(get_engine()) as s:
        return {x.slug: x for x in s.exec(select(Service)).all()}


def test_yaml_round_trip_keeps_the_tree_and_the_node(registry: Path) -> None:
    with Session(get_engine()) as s:
        parsed, _ = read_yaml_file(registry)
        reconcile_yaml_into_db(s, registry)
        s.commit()
    rows = _services()
    assert rows["nginx"].parent_id == rows["portainer"].id
    assert rows["nginx"].node_id == rows["portainer"].node_id  # inherited
    assert rows["nginx"].guest_kind is GuestKind.NONE
    assert rows["nginx"].controller["container"] == "nginx"
    assert rows["nginx"].backup_gate.value == "required"
    assert rows["nginx"].recovery == {"enabled": True, "max_attempts": 2}

    with Session(get_engine()) as s:
        save_registry_to_yaml(s, registry)
        s.commit()
    doc = yaml.safe_load(registry.read_text())
    by = {x["slug"]: x for x in doc["services"]}
    assert by["nginx"]["parent"] == "portainer" and "node" not in by["nginx"]
    assert "parent" not in by["portainer"] and "controller" not in by["portainer"]
    assert by["nginx"]["proxy_hosts"][0]["hostname"] == "nginx.example.com"
    assert parsed.services[1].parent == "portainer"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"nginx": {"parent": "ghost"}}, "has parent 'ghost'"),
        ({"nginx": {"parent": "nginx"}}, "its own parent"),
        ({"nginx": {"guest_kind": "vm", "guest_id": 5}}, "has its own guest"),
        ({"nginx": {"backup_policy": "daily"}}, "has a backup_policy"),
        ({"portainer": {"parent": "nginx"}}, "cycle"),
    ],
)
def test_validate_references_rejects_bad_trees(patch: dict[str, Any], message: str) -> None:
    doc = yaml.safe_load(REGISTRY)
    doc["backup_policies"] = [{"name": "daily"}]
    for svc in doc["services"]:
        svc.update(patch.get(svc["slug"], {}))
    with pytest.raises(ValueError, match=message):
        validate_references(RegistryFile.model_validate(doc))


def test_tree_index_walks_and_inherits(registry: Path) -> None:
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, registry)
        s.commit()
        idx = TreeIndex.build(s)
        root, child = idx.by_slug["portainer"], idx.by_slug["nginx"]
        assert [x.slug for x, _ in idx.walk()] == ["portainer", "nginx"]
        assert idx.root(child) is root and idx.depth(child) == 2
        eg = effective_guest(s, child)
        assert eg.guest_id == 101 and eg.root_slug == "portainer" and eg.has_guest
        root.enabled = False
        s.add(root)
        s.flush()
        assert is_served(s, child) is False  # a disabled root parks its subtree


@pytest.fixture
def client(registry: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, registry)
        s.commit()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_api_creates_a_sub_service_on_the_roots_node(client: TestClient) -> None:
    r = client.post(
        "/api/registry/services",
        json={"slug": "redis", "name": "Redis", "parent": "portainer", "backup_gate": "required"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["parent"] == "portainer" and body["root"] == "portainer"
    assert body["node"]["name"] == "pve1" and body["depth"] == 2
    assert body["guest_kind"] == "none" and body["backup_gate"] == "required"
    parent = client.get("/api/registry/services/portainer").json()
    assert {c["slug"] for c in parent["children"]} == {"nginx", "redis"}

    bad = client.post(
        "/api/registry/services",
        json={"slug": "x", "name": "X", "parent": "portainer", "guest_kind": "vm", "guest_id": 3},
    )
    assert bad.status_code == 400 and "root's guest" in bad.text
    bad = client.post("/api/registry/services", json={"slug": "y", "name": "Y", "parent": "ghost"})
    assert bad.status_code == 400
    bad = client.post("/api/registry/services", json={"slug": "z", "name": "Z"})
    assert bad.status_code == 400  # a root needs a node


def test_update_rules_for_children_and_loops(client: TestClient) -> None:
    child = client.put("/api/registry/services/nginx", json={"guest_id": 9})
    assert child.status_code == 400 and "inherits" in child.text
    loop = client.put("/api/registry/services/portainer", json={"parent": "nginx"})
    assert loop.status_code == 400 and "loop" in loop.text
    ok = client.put("/api/registry/services/nginx", json={"name": "web", "backup_gate": "ignore"})
    assert ok.status_code == 200 and ok.json()["backup_gate"] == "ignore"
    bad = client.put("/api/registry/services/nginx", json={"backup_gate": "maybe"})
    assert bad.status_code == 400


def test_moving_a_root_to_another_node_moves_its_children(client: TestClient) -> None:
    with Session(get_engine()) as s:
        s.add(Node(name="pve2", always_on=True))
        s.commit()
        pve2 = s.exec(select(Node).where(Node.name == "pve2")).one().id
    r = client.put("/api/registry/services/portainer", json={"node_id": pve2})
    assert r.status_code == 200
    assert _services()["nginx"].node_id == pve2


def test_reparenting_clears_the_guest_and_detaching_makes_a_root(client: TestClient) -> None:
    client.post(
        "/api/registry/services",
        json={
            "slug": "solo",
            "name": "Solo",
            "node_id": _services()["portainer"].node_id,
            "guest_kind": "vm",
            "guest_id": 300,
        },
    )
    r = client.put("/api/registry/services/solo", json={"parent": "portainer"})
    assert r.status_code == 200
    solo = _services()["solo"]
    assert solo.parent_id == _services()["portainer"].id and solo.guest_id is None
    back = client.put("/api/registry/services/solo", json={"parent": ""})
    assert back.status_code == 200 and back.json()["parent"] is None


def test_deleting_a_parent_needs_cascade(client: TestClient) -> None:
    r = client.delete("/api/registry/services/portainer")
    assert r.status_code == 409 and "nginx" in r.text
    ok = client.delete("/api/registry/services/portainer?cascade=true")
    assert ok.status_code == 200
    assert _services() == {}
    with Session(get_engine()) as s:
        assert s.exec(select(ProxyHost)).all() == [] or all(
            p.service_id is None for p in s.exec(select(ProxyHost)).all()
        )


def test_list_reports_parent_and_depth(client: TestClient) -> None:
    rows = {r["slug"]: r for r in client.get("/api/registry/services").json()}
    assert rows["nginx"]["parent"] == "portainer" and rows["nginx"]["depth"] == 2
    assert rows["portainer"]["parent"] is None


# --- health and the guest ------------------------------------------------------


def test_a_sub_service_is_down_when_its_roots_guest_is_stopped(client: TestClient) -> None:
    proxmox = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    proxmox.simulated_guests = [
        Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="docker", status="stopped")
    ]
    proxmox.simulated_cluster = ClusterStatus(nodes=[ClusterNode("pve1", online=True)])
    out = client.post("/api/infra/resync", json={}).json()
    by = {r["slug"]: r for r in out["results"]}
    assert by["nginx"]["verdict"] == HealthState.DOWN.value
    assert by["portainer"]["verdict"] == HealthState.DOWN.value
    assert by["nginx"]["guest"] == "stopped"


def test_a_disabled_root_is_not_scanned_and_blocks_wakes(client: TestClient) -> None:
    client.put("/api/registry/services/portainer", json={"enabled": False})
    out = client.post("/api/infra/resync", json={}).json()
    assert out["results"] == []  # nothing served, nothing scanned
    page = client.get("/maintenance/nginx")
    assert "Not managed" in page.text


# --- proxy hosts follow the tree ----------------------------------------------


def test_disabling_a_sub_service_parks_its_hosts_but_not_their_own_switch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROBE_PROXY_BASE_URL", "https://10.0.0.2")
    config_module.reset_settings_cache()
    nginx_id = _services()["nginx"].id
    created = client.post(
        "/api/proxy-hosts", json={"hostname": "web.example.com", "service_id": nginx_id}
    )
    assert created.status_code == 201, created.text
    host_id = created.json()["id"]

    def host() -> dict[str, Any]:
        return next(h for h in client.get("/api/proxy-hosts").json() if h["id"] == host_id)

    def probe_enabled() -> bool:
        with Session(get_engine()) as s:
            p = s.exec(select(Probe).where(Probe.name == "traefik:web.example.com")).one()
            return p.enabled

    assert host()["served"] is True and probe_enabled() is True
    client.put("/api/registry/services/nginx", json={"enabled": False})
    h = host()
    assert h["enabled"] is True and h["served"] is False  # own switch untouched
    assert probe_enabled() is False
    client.put("/api/registry/services/nginx", json={"enabled": True})
    assert host()["served"] is True and probe_enabled() is True

    # A disabled ROOT parks its children's hosts too.
    client.put("/api/registry/services/portainer", json={"enabled": False})
    assert host()["served"] is False and probe_enabled() is False


def test_a_sub_service_cannot_be_backed_up_on_its_own(client: TestClient) -> None:
    from orchestrator.pipelines.backup import BackupError, BackupPipeline

    with Session(get_engine()) as s, pytest.raises(BackupError, match="sub-service"):
        BackupPipeline._resolve(object.__new__(BackupPipeline), s, "nginx")


def test_services_list_shows_the_sub_service_under_its_parent(client: TestClient) -> None:
    rows = client.get("/api/views/services").json()["rows"]
    slugs = [r["slug"] for r in rows]
    assert slugs.index("portainer") < slugs.index("nginx")
    by = {r["slug"]: r for r in rows}
    assert by["nginx"]["parent"] == "portainer"
    assert by["nginx"]["depth"] == by["portainer"]["depth"] + 1
    page = client.get("/services/nginx").text
    assert '"parent_choices": ["portainer"]' in page
    assert 'key: "parent", label: "Part of"' in client.get("/static/service.js").text
    # a root cannot be made a child of its own subtree
    assert '"parent_choices": []' in client.get("/services/portainer").text


def test_cli_parent_and_gate(registry: Path) -> None:
    from click.testing import CliRunner

    from orchestrator.cli import cli

    out = CliRunner().invoke(
        cli, ["service", "update", "nginx", "--backup-gate", "ignore", "--parent", "portainer"]
    )
    assert out.exit_code == 0, out.output
    doc = yaml.safe_load(registry.read_text())
    nginx = next(s for s in doc["services"] if s["slug"] == "nginx")
    assert nginx["backup_gate"] == "ignore" and nginx["parent"] == "portainer"
    bad = CliRunner().invoke(cli, ["service", "update", "nginx", "--parent", "nginx"])
    assert bad.exit_code != 0
    detach = CliRunner().invoke(cli, ["service", "update", "nginx", "--parent", ""])
    assert detach.exit_code == 0


# --- backups wait for required sub-services -----------------------------------------


async def _run_backup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, verdicts: dict[str, HealthState]
) -> Any:
    """Back up 'portainer' with the health engine answering from ``verdicts``."""
    from orchestrator.domain.schemas import ServiceVerdict
    from orchestrator.health.engine import HealthEngine
    from orchestrator.pipelines.backup import BackupPipeline

    seen: list[str] = []

    async def scan(self: Any, session: Any, service: Any, **kw: Any) -> ServiceVerdict:
        seen.append(service.slug)
        return ServiceVerdict(
            state=verdicts[service.slug], reason=f"{service.slug} is test", probe_results=[]
        )

    monkeypatch.setattr(HealthEngine, "scan", scan)
    monkeypatch.setattr(HealthEngine, "can_verify", lambda self, session, service: True)
    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    settings = config_module.get_settings().model_copy(
        update={"wake_poll_interval_s": 0.001, "wake_timeout_s": 1}
    )
    pipe = BackupPipeline(
        proxmox=adapters.proxmox, pbs=adapters.pbs, settings=settings, power=adapters.power
    )
    with Session(get_engine()) as s:
        try:
            run = await pipe.run_for_service(s, "portainer", wake=True)
            return [step["name"] for step in run.steps], seen, None
        except Exception as exc:
            return None, seen, exc


async def test_a_required_sub_service_that_is_not_healthy_refuses_the_backup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/registry/services/nginx", json={"backup_gate": "required"})
    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    adapters.proxmox.simulated_guests = [
        Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="d", status="running")
    ]
    run, seen, err = await _run_backup(
        client, monkeypatch, {"portainer": HealthState.HEALTHY, "nginx": HealthState.FAILED}
    )
    assert run is None and "required sub-service 'nginx' is FAILED" in str(err)
    assert "nginx" in seen
    # the same sub, ignored: the backup goes ahead (and is judged only on the root)
    client.put("/api/registry/services/nginx", json={"backup_gate": "ignore"})
    run, seen, err = await _run_backup(
        client, monkeypatch, {"portainer": HealthState.HEALTHY, "nginx": HealthState.FAILED}
    )
    assert err is None and "nginx" not in seen


async def test_a_required_sub_service_that_is_healthy_lets_the_backup_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/registry/services/nginx", json={"backup_gate": "required"})
    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    adapters.proxmox.simulated_guests = [
        Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="d", status="running")
    ]
    names, _seen, err = await _run_backup(
        client, monkeypatch, {"portainer": HealthState.HEALTHY, "nginx": HealthState.HEALTHY}
    )
    assert err is None, err
    assert "sub_gate:nginx" in names and names.index("integrity_gate") < names.index(
        "sub_gate:nginx"
    )
    assert "sub_start:nginx" in names  # it has a controller in the fixture registry


def test_the_plan_lists_required_sub_services(client: TestClient) -> None:
    client.put("/api/registry/services/nginx", json={"backup_gate": "required"})
    plan = client.get("/api/backup/plan?slug=portainer").json()
    assert plan["required_subs"] == [
        {"slug": "nginx", "name": "nginx container", "controller": "portainer", "can_start": True}
    ]
    client.put("/api/registry/services/nginx", json={"enabled": False})
    assert client.get("/api/backup/plan?slug=portainer").json()["required_subs"] == []
