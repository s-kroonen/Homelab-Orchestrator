"""The data-sources API: pulling from Proxmox, addresses, Traefik Manager and Portainer, and
formatting routes through the API (plan, run, the run record)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.proxmox.base import Guest
from orchestrator.db import session as session_module
from orchestrator.db.models import ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, RouterProvider
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.web.app import create_app
from tests.sim_tm import SimTM
from tests.test_proxy_entrypoints_api import TM_ENV
from tests.test_proxy_hosts import _login_as_first_admin, _patch_tm_transport, _tm_client

REGISTRY = """
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: web-vm
    name: Web VM
    node: pve1
    guest_kind: vm
    guest_id: 101
  - slug: db-vm
    name: DB VM
    node: pve1
    guest_kind: vm
    guest_id: 102
    ip_address: 10.0.0.22
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    with TestClient(create_app()) as c:
        c.app.state.adapters.proxmox.simulated_guests = [  # type: ignore[attr-defined]
            Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="web", status="running"),
            Guest(node="pve1", vmid=102, kind=GuestKind.VM, name="db", status="running"),
            Guest(node="pve1", vmid=200, kind=GuestKind.VM, name="unregistered", status="stopped"),
        ]
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_everything_needs_an_admin(client: TestClient) -> None:
    anon = TestClient(client.app)
    for path in ("overview", "proxmox", "ips", "traefik", "portainer"):
        assert anon.get(f"/api/sources/{path}").status_code == 401


def test_overview_counts_what_is_known(client: TestClient) -> None:
    out = client.get("/api/sources/overview").json()
    assert out["proxmox"] == {"nodes": 1, "guests": 2}
    assert out["ips"]["services"] == 2 and out["ips"]["missing"] >= 0
    assert out["portainer"]["configured"] is True  # the dry-run Portainer
    assert out["refreshed"] == {}


def test_proxmox_lists_nodes_and_guests_and_refresh_pulls_them(client: TestClient) -> None:
    got = client.get("/api/sources/proxmox").json()
    assert [n["name"] for n in got["nodes"]] == ["pve1"]
    assert {g["vmid"] for g in got["guests"]} == {101, 102, 200}
    assert next(g for g in got["guests"] if g["vmid"] == 200)["service_slug"] is None
    done = client.post("/api/sources/proxmox/refresh").json()
    assert {"addresses", "nodes", "guests"} <= set(done)
    assert [u["vmid"] for u in done["guests"]["unregistered_guests"]] == [200]
    assert client.get("/api/sources/overview").json()["refreshed"]["proxmox"]


def test_ips_list_and_bulk_discovery_never_touches_a_manual_address(client: TestClient) -> None:
    rows = client.get("/api/sources/ips").json()
    by = {r["slug"]: r for r in rows}
    assert by["db-vm"]["manual_ip"] == "10.0.0.22" and by["db-vm"]["effective_ip"] == "10.0.0.22"
    out = client.post("/api/sources/ips/discover", json={"only_missing": True}).json()
    assert [r["slug"] for r in out["results"]] == ["web-vm"]  # the one without an address
    out = client.post("/api/sources/ips/discover", json={}).json()
    assert out["scanned"] == 2
    assert (
        next(r for r in client.get("/api/sources/ips").json() if r["slug"] == "db-vm")["manual_ip"]
        == "10.0.0.22"
    )
    assert client.post("/api/sources/ips/discover", json={"node": "ghost"}).status_code == 404


def test_portainer_lists_stacks(client: TestClient) -> None:
    out = client.get("/api/sources/portainer").json()
    assert out["configured"] and {s["name"] for s in out["stacks"]} >= {"dev-xymbols"}


# -- Traefik --------------------------------------------------------------------------------------


@pytest.fixture
def tm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: Service) -> Any:
    sim = SimTM(files=("dynamic.yml", "legacy.yml"), config_dir=True)
    sim.add(
        "legacy.yml",
        "nas",
        "nas.example.com",
        upstream="http://10.0.0.15:80",
        middlewares=("redirect-to-https", "hsts-headers"),
        resolver="letsencrypt",
    )
    sim.add("legacy.yml", "hand", "hand.example.com", upstream="http://10.0.0.16:80")
    client = _tm_client(
        monkeypatch,
        **{**TM_ENV, "STATE_DIR": str(tmp_path), "TRAEFIK_MANAGER_CONFIG_FILE": "apps.yml"},
    )
    _patch_tm_transport(monkeypatch, sim)
    with Session(get_engine()) as s:
        svc = s.exec(select(Service)).one()
        s.add(
            ProxyHost(
                hostname="nas.example.com",
                upstream="http://10.0.0.15:80",
                router_provider=RouterProvider.TRAEFIK,
                service_id=svc.id,
                entry_points=["websecure"],
                cert_resolver="letsencrypt",
            )
        )
        s.commit()
    yield client, sim
    client.__exit__(None, None, None)


def test_traefik_view_pairs_routes_with_records(tm: Any) -> None:
    client, _ = tm
    out = client.get("/api/sources/traefik").json()
    assert out["readable"] is True
    nas = next(r for r in out["routes"] if r["route"] == "nas")
    assert nas["host_id"] is not None and nas["owned"] is False and nas["file"] == "legacy.yml"
    hand = next(r for r in out["routes"] if r["route"] == "hand")
    assert hand["host_id"] is None  # no record: it is "discovered"
    assert out["no_route"] == []


def test_import_pulls_unknown_routes_into_records(tm: Any) -> None:
    client, _ = tm
    done = client.post("/api/sources/traefik/import")
    assert done.status_code == 200, done.text
    assert client.get("/api/sources/overview").json()["refreshed"]["traefik"]


def _wait(client: TestClient, run_id: int) -> dict[str, Any]:
    for _ in range(200):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def test_format_plan_then_run_rewrites_the_route_and_records_the_run(tm: Any) -> None:
    client, sim = tm
    plan = client.post("/api/sources/traefik/format/plan", json={}).json()
    item = next(i for i in plan["items"] if i["hostname"] == "nas.example.com")
    assert item["status"] == "ready" and item["dropped_middlewares"] == ["redirect-to-https"]
    started = client.post(
        "/api/sources/traefik/format/run",
        json={"items": [{"hostname": item["hostname"], "fingerprint": item["fingerprint"]}]},
    )
    assert started.status_code == 202, started.text
    run = _wait(client, started.json()["run_id"])
    assert run["status"] == "succeeded", run
    assert run["kind"] == "format"
    assert sim.file_of("orchestrator-nas-example-com") == "apps.yml" and sim.file_of("nas") is None
    assert (
        client.post("/api/sources/traefik/format/plan", json={}).json()["items"][0]["status"]
        == "standard"
    )


def test_a_stale_plan_is_refused_and_a_second_run_cannot_overlap(tm: Any) -> None:
    client, sim = tm
    plan = client.post("/api/sources/traefik/format/plan", json={}).json()
    item = next(i for i in plan["items"] if i["hostname"] == "nas.example.com")
    sim.files["legacy.yml"]["nas"]["mws"].append("changed")
    run = _wait(
        client,
        client.post(
            "/api/sources/traefik/format/run",
            json={"items": [{"hostname": item["hostname"], "fingerprint": item["fingerprint"]}]},
        ).json()["run_id"],
    )
    assert run["status"] == "failed"
    assert any(
        "changed since the plan" in (s["detail"] or {}).get("reason", "") for s in run["steps"]
    )
    assert sim.file_of("nas") == "legacy.yml"


def test_kept_middlewares_can_be_chosen_per_host(tm: Any) -> None:
    client, sim = tm
    item = next(
        i
        for i in client.post("/api/sources/traefik/format/plan", json={}).json()["items"]
        if i["hostname"] == "nas.example.com"
    )
    run = _wait(
        client,
        client.post(
            "/api/sources/traefik/format/run",
            json={
                "items": [
                    {
                        "hostname": item["hostname"],
                        "fingerprint": item["fingerprint"],
                        "middlewares": [],
                    }
                ]
            },
        ).json()["run_id"],
    )
    assert run["status"] == "succeeded", run
    assert (
        sim.files["apps.yml"]["orchestrator-nas-example-com"]["mws"] == []
    )  # hsts dropped on purpose
