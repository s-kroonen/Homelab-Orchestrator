"""OR-78: the source pages. All sources, one page per source with its tabs, the sidebar's
Sources section, and the editors that moved off the old Connections page."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator import config as config_module
from orchestrator.connectors import status as source_status
from orchestrator.db import session as session_module
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    source_status.forget()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c
    source_status.forget()


def _pve(client: TestClient, ident: str, host: str, **kw: Any) -> None:
    r = client.post(
        "/api/connections",
        json={
            "kind": "proxmox",
            "id": ident,
            "name": ident,
            "settings": {"host": host, "token_id": "orchestrator@pve!api"},
            "secrets": {"token_secret": "s3cr3t-" + ident},
            **kw,
        },
    )
    assert r.status_code == 201, r.text


def _proxy(client: TestClient, ident: str) -> None:
    r = client.post(
        "/api/connections",
        json={
            "kind": "proxy",
            "id": ident,
            "name": ident,
            "settings": {"url": "https://traefik-manager.example.lan"},
            "secrets": {"api_key": "proxy-key"},
        },
    )
    assert r.status_code == 201, r.text


def test_the_api_needs_an_admin() -> None:
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        assert c.get("/api/sources").status_code == 401
        r = c.get("/sources", follow_redirects=False)
        assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]


def test_all_sources_lists_every_source_with_its_status(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10", default=True)
    _pve(client, "cluster-b", "10.0.1.10")
    _proxy(client, "gateway")
    rows = client.get("/api/sources").json()
    assert [r["id"] for r in rows] == ["cluster-a", "cluster-b", "gateway"]
    a = rows[0]
    assert a["href"] == "/sources/cluster-a" and a["kind_title"] and a["address"] == "10.0.0.10"
    assert a["status"]["state"] == "unknown"  # not checked yet: pages never wait for a check

    checked = client.post("/api/sources/check").json()
    assert all(r["status"]["checked_at"] for r in checked)
    kinds = {k["kind"]: k for k in client.get("/api/sources/kinds").json()}
    assert kinds["proxmox"]["multi"] and kinds["proxmox"]["fields"]
    assert "ntfy" not in kinds and "ssh" not in kinds  # channels and SSH are not sources

    page = client.get("/sources")
    assert page.status_code == 200 and "sources_index.js" in page.text
    assert "s3cr3t" not in page.text and "proxy-key" not in page.text


def test_the_sidebar_folds_a_kind_with_several_sources(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10", default=True)
    _pve(client, "cluster-b", "10.0.1.10")
    _proxy(client, "gateway")
    html = client.get("/sources/cluster-b").text
    nav = html[html.index('<nav id="sidebar-nav"') : html.index("</nav>")]
    # Proxmox has two: a fold, open because this page is one of them, both listed under it
    assert 'data-fold="Proxmox" open' in nav
    assert 'href="/sources/cluster-a"' in nav and 'href="/sources/cluster-b"' in nav
    assert 'class="nav-src nav-sub current"' in nav
    # the proxy has one: a single line "Proxy · gateway"
    assert 'data-fold="Proxy"' not in nav and "· gateway" in nav
    assert "/infra/connections" not in nav


def test_a_source_page_has_its_tabs_and_never_its_secrets(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10")
    page = client.get("/sources/cluster-a")
    assert page.status_code == 200 and "source.js" in page.text and "conn_drawer.js" in page.text
    assert "s3cr3t" not in page.text
    d = client.get("/api/sources/cluster-a").json()
    assert [t["view"] for t in d["tabs"]] == [
        "overview",
        "resource:nodes",
        "resource:guests",
        "settings",
        "raw",
    ]
    assert d["connection"]["secrets_set"]["token_secret"] is True
    assert d["multi"] is True and d["fields"]
    nodes = d["resources"]["nodes"]
    assert nodes["columns"][0]["key"] == "name" and nodes["chips"]
    assert {e["key"] for e in d["raw"]} >= {"cluster", "guests"}
    assert client.get("/sources/nope").status_code == 404
    assert client.get("/api/sources/nope").status_code == 404


def test_overview_resources_and_raw_answer(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10")
    ov = client.get("/api/sources/cluster-a/overview").json()
    assert set(ov) == {"tiles", "attention", "facts"}
    assert all(v != "s3cr3t-cluster-a" for _, v in ov["facts"])
    rows = client.get("/api/sources/cluster-a/resources/nodes").json()
    assert rows["key"] == "nodes" and isinstance(rows["rows"], list)
    assert client.get("/api/sources/cluster-a/resources/nope").status_code == 404
    raw = client.get("/api/sources/cluster-a/raw/version").json()
    assert raw["key"] == "version" and "data" in raw
    assert client.get("/api/sources/cluster-a/raw/nope").status_code == 404
    tested = client.post("/api/sources/cluster-a/test").json()
    assert tested["id"] == "cluster-a" and tested["status"]["checked_at"]


def test_a_switched_off_source_shows_nothing_but_its_settings(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10", enabled=False)
    assert client.get("/api/sources/cluster-a").json()["enabled"] is False
    assert client.get("/api/sources/cluster-a/overview").status_code == 409
    assert client.get("/api/sources/cluster-a/resources/nodes").status_code == 409
    row = client.post("/api/sources/cluster-a/test").json()
    assert row["status"]["state"] == "off"


def test_channels_and_ssh_are_edited_where_they_are_used(client: TestClient) -> None:
    notes = client.get("/notifications").text
    assert "conn_drawer.js" in notes and "/infra/connections" not in notes
    system = client.get("/system").text
    assert "conn_drawer.js" in system and 'id="ssh-facts"' in system
    assert "window.ConnDrawer.open" in client.get("/static/notifications.js").text
    assert "window.ConnDrawer.open" in client.get("/static/system.js").text
