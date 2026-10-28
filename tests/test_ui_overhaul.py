"""The new layout (docs/design/README.md): one save model, the header's pending pill and search,
and the Services / Nodes & guests / Admins & devices pages with their JSON views."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import Node, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.registry import autosave
from orchestrator.web.app import create_app
from tests.test_auth import _register


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


@pytest.fixture
def client(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(app) as c:
        r = _register(c, monkeypatch, username="admin", credential_id=b"cred-1")
        assert r.status_code == 200, r.text
        yield c


def _seed() -> None:
    with Session(get_engine()) as s:
        node = Node(name="node-a", always_on=True, address="10.0.0.5")
        s.add(node)
        s.commit()
        s.refresh(node)
        media = Service(
            slug="media", name="Media", node_id=node.id, guest_kind=GuestKind.VM, guest_id=101
        )
        s.add(media)
        s.commit()
        s.refresh(media)
        s.add(Service(slug="media-db", name="Media DB", parent_id=media.id))
        s.add(
            ProxyHost(hostname="tv.example.lan", upstream="http://10.0.0.9:80", service_id=media.id)
        )
        s.commit()


# --- pages --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/services", "/nodes", "/admins"])
def test_new_pages_need_a_login(app: Any, path: str) -> None:
    with TestClient(app, follow_redirects=False) as anon:
        r = anon.get(path)
    assert r.status_code == 303 and r.headers["location"] == f"/login?next={path}"


def test_services_page_paints_with_its_data(client: TestClient) -> None:
    _seed()
    html = client.get("/services").text
    # The rows are in the page: the first paint needs no request.
    assert 'id="initial"' in html and '"slug": "media-db"' in html
    assert "/static/services.js?v=" in html and "/static/objects.js?v=" in html


def test_services_view_shapes_rows_for_the_table(client: TestClient) -> None:
    _seed()
    view = client.get("/api/views/services").json()
    rows = {r["slug"]: r for r in view["rows"]}
    assert [r["slug"] for r in view["rows"]] == ["media", "media-db"]  # parents first
    media, db = rows["media"], rows["media-db"]
    assert media["state"] == {"cls": "off", "label": "unknown", "why": "not checked yet"}
    assert media["runs_on"]["main"] == "node-a" and media["runs_on"]["sub"].startswith("vm 101")
    assert db["parent"] == "media" and db["runs_on"]["sub"] == "sub-service"
    assert db["backup"]["main"] == "with parent"
    assert media["backup"]["main"] == "no policy" and media["not_backed_up"] is True
    assert view["nodes"] == [{"id": 1, "name": "node-a"}]


def test_a_disabled_service_reads_off_not_failed(client: TestClient) -> None:
    _seed()
    client.put("/api/registry/services/media", json={"enabled": False})
    rows = {r["slug"]: r for r in client.get("/api/views/services").json()["rows"]}
    assert rows["media"]["state"]["cls"] == "off" and rows["media"]["state"]["label"] == "disabled"


def test_nodes_view_counts_services_by_their_root(client: TestClient) -> None:
    _seed()
    [node] = client.get("/api/views/nodes").json()["nodes"]
    assert node["name"] == "node-a" and node["services"] == 2
    assert node["service_slugs"] == ["media"]  # what a power-off confirm lists
    html = client.get("/nodes").text
    assert 'href="/sources#proxmox"' in html and "/static/nodes.js?v=" in html


def test_admins_page_holds_devices_and_invites(client: TestClient) -> None:
    html = client.get("/admins").text
    assert 'id="add-device"' in html and 'id="new-invite"' in html
    assert "/static/webauthn.js?v=" in html
    # ...and they left the dashboard.
    assert "Invite another admin" not in client.get("/").text


# --- header -------------------------------------------------------------------------------------


def test_search_lists_services_nodes_hosts_and_pages(client: TestClient) -> None:
    _seed()
    items = client.get("/api/search").json()["items"]
    by_kind = {(i["kind"], i["label"]): i for i in items}
    assert by_kind[("service", "Media")]["href"] == "/services/media"
    assert ("node", "node-a") in by_kind
    assert by_kind[("proxy host", "tv.example.lan")]["href"] == "/proxy-hosts?q=tv.example.lan"
    assert by_kind[("page", "Nodes & guests")]["href"] == "/nodes"


def test_pending_says_what_is_not_written_when_autosave_is_off(client: TestClient) -> None:
    _seed()  # written straight to the database: services.yaml does not have it yet
    p = client.get("/api/pending").json()
    assert p["autosave"] is False and p["yaml_dirty"] is True
    assert "Changes not written to services.yaml yet" in p["items"]
    # The pill's button: write (and push) now.
    after = client.post("/api/pending/retry").json()
    assert after["yaml_dirty"] is False and after["save_error"] is None
    assert "media-db" in Path(get_settings().services_yaml_path).read_text()


# --- the autosaver ---------------------------------------------------------------------------------


def test_run_once_writes_the_yaml_and_keeps_the_previous_one(client: TestClient) -> None:
    _seed()
    settings = get_settings()
    yaml_path = Path(settings.services_yaml_path)
    yaml_path.write_text("version: 1\nservices: []\nnodes: []\n")
    saver = autosave.Autosaver(settings=settings, adapters=client.app.state.adapters)
    state = asyncio.run(saver.run_once(push=False))
    assert state.save_error is None and state.saved_at
    assert "media-db" in yaml_path.read_text()
    [kept] = list(autosave.history_dir(settings).glob("services-*.yaml"))
    assert kept.read_text() == "version: 1\nservices: []\nnodes: []\n"
    # Nothing changed since: no second copy.
    asyncio.run(saver.run_once(push=False))
    assert len(list(autosave.history_dir(settings).glob("services-*.yaml"))) == 1


def test_history_keeps_only_the_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(autosave, "HISTORY_KEEP", 3)
    Path(settings.services_yaml_path).write_text("x")
    for _ in range(5):
        autosave.keep_previous(settings)
    assert len(list(autosave.history_dir(settings).glob("services-*.yaml"))) == 3


def test_a_change_is_written_to_services_yaml_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGISTRY_AUTOSAVE", "true")
    monkeypatch.setattr(autosave, "QUIET_S", 0.05)
    from orchestrator import config as config_module

    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    app = create_app()
    with TestClient(app) as c:
        assert _register(c, monkeypatch, username="admin", credential_id=b"c").status_code == 200
        r = c.post("/api/registry/nodes", json={"name": "node-z", "always_on": True})
        assert r.status_code == 201, r.text
        yaml_path = Path(get_settings().services_yaml_path)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if yaml_path.exists() and "node-z" in yaml_path.read_text():
                break
            time.sleep(0.05)
        assert "node-z" in yaml_path.read_text()
        p = c.get("/api/pending").json()
        assert p["autosave"] is True and p["yaml_dirty"] is False
