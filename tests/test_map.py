"""The map: /api/map builds the picture, /map serves the page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.db import session as session_module
from orchestrator.db.models import Service
from orchestrator.db.session import get_engine
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

REGISTRY = """
version: 1
nodes:
  - {name: pve1, always_on: true}
backup_policies:
  - {name: daily}
services:
  - slug: portainer
    name: Portainer
    node: pve1
    guest_kind: vm
    guest_id: 101
    backup_policy: daily
    depends_on: [db]
    proxy_hosts:
      - {hostname: portainer.example.com, upstream: "http://10.0.0.5:9443"}
  - slug: nginx
    name: nginx container
    parent: portainer
    backup_gate: required
    proxy_hosts:
      - {hostname: nginx.example.com, upstream: "http://10.0.0.5:8080"}
  - slug: db
    name: Database
    node: pve1
    guest_kind: ct
    guest_id: 200
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def _flat(svcs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in svcs:
        out[s["slug"]] = s
        out.update(_flat(s["children"]))
    return out


def _edges(data: dict[str, Any], kind: str) -> set[tuple[str, str]]:
    return {(e["from"], e["to"]) for e in data["edges"] if e["kind"] == kind}


def test_map_nests_services_in_guests_on_nodes(client: TestClient) -> None:
    data = client.get("/api/map").json()
    assert data["counts"] == {"nodes": 1, "services": 2, "sub_services": 1, "hosts": 2}
    (node,) = data["nodes"]
    assert node["id"] == "node:pve1" and node["always_on"] is True
    guests = {g["id"]: g for g in node["guests"]}
    assert set(guests) == {"guest:pve1/101", "guest:pve1/200"}
    assert guests["guest:pve1/101"]["kind"] == "vm"
    (portainer,) = guests["guest:pve1/101"]["services"]
    assert portainer["slug"] == "portainer" and portainer["kind"] == "service"
    (nginx,) = portainer["children"]
    assert nginx["kind"] == "sub" and nginx["backup_gate"] == "required"
    assert nginx["id"] == "svc:nginx"


def test_map_edges_show_traffic_dependencies_and_backups(client: TestClient) -> None:
    data = client.get("/api/map").json()
    assert _edges(data, "proxy") == {
        ("host:portainer.example.com", "svc:portainer"),
        ("host:nginx.example.com", "svc:nginx"),
    }
    assert _edges(data, "depends") == {("svc:portainer", "svc:db")}
    assert _edges(data, "backup") == {("svc:portainer", "pbs")}
    assert _edges(data, "waits") == {("svc:portainer", "svc:nginx")}
    hosts = {h["hostname"]: h for h in data["entry"]["hosts"]}
    assert hosts["nginx.example.com"]["service"] == "nginx"


def test_a_disabled_parent_parks_its_subtree_and_switches_the_lines_off(
    client: TestClient,
) -> None:
    with Session(get_engine()) as s:
        row = s.exec(select(Service).where(Service.slug == "portainer")).one()
        row.enabled = False
        s.add(row)
        s.commit()
    data = client.get("/api/map").json()
    flat = _flat([x for g in data["nodes"][0]["guests"] for x in g["services"]])
    assert flat["portainer"]["verdict"] == "disabled"
    assert flat["nginx"]["verdict"] == "parked"
    proxy = {e["to"]: e for e in data["edges"] if e["kind"] == "proxy"}
    assert proxy["svc:nginx"]["off"] is True
    # a parked sub-service is not waited for during a backup
    assert _edges(data, "waits") == set()


def test_map_api_needs_a_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICES_YAML_PATH", str(tmp_path / "services.yaml"))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with TestClient(create_app()) as anon:
        assert anon.get("/api/map").status_code == 401
        r = anon.get("/map", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_map_page_renders_and_is_in_the_nav(client: TestClient) -> None:
    r = client.get("/map")
    assert r.status_code == 200
    assert 'id="map-root"' in r.text and "/static/map.js" in r.text
    assert 'href="/map"' in r.text
    # the Map wireframe's controls: find, problems only, lines for the selection or all
    assert 'id="map-q"' in r.text and 'id="map-problems"' in r.text
    assert 'data-lines="selected"' in r.text and 'data-lines="all"' in r.text
    assert 'id="map-side"' in r.text and "page-map" in r.text
    # painted from the database at once: the map's data is in the page
    assert '"slug": "portainer"' in r.text and '"edges":' in r.text
    js = client.get("/static/map.js")
    assert js.status_code == 200
    assert "/api/views/map" in js.text and "/api/infra/nodes/status" in js.text
    assert "location.reload" not in js.text


def test_map_view_carries_what_the_sidebar_needs(client: TestClient) -> None:
    data = client.get("/api/views/map").json()
    assert data["map"]["counts"]["services"] == 2
    rows = {r["slug"]: r for r in data["view"]["rows"]}
    assert rows["portainer"]["state"]["label"] == "unknown"
    assert rows["portainer"]["id"] is not None
    assert [n["name"] for n in data["nodes"]] == ["pve1"]
    assert [p["name"] for p in data["policies"]] == ["daily"]
    assert set(data["policies"][0]) == {"name", "next_run", "restore_power_state"}
    hosts = {h["hostname"]: h for h in data["map"]["entry"]["hosts"]}
    assert isinstance(hosts["portainer.example.com"]["pk"], int)


def test_services_without_a_node_are_on_the_map(client: TestClient) -> None:
    with Session(get_engine()) as s:
        s.add(Service(slug="loose", name="Loose"))
        s.commit()
    data = client.get("/api/map").json()
    assert [x["slug"] for x in data["unplaced"]] == ["loose"]
    assert all(
        x["slug"] != "loose"
        for n in data["nodes"]
        for x in n["loose"] + [y for g in n["guests"] for y in g["services"]]
    )
