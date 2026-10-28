"""Connectivity per connection: one reachable/not row each, and a page for what is up inside."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator import config as config_module
from orchestrator.db import session as session_module
from orchestrator.db.models import Node
from orchestrator.db.session import get_engine
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def _add(
    client: TestClient, kind: str, ident: str, settings: dict, secrets: dict, **kw: Any
) -> None:
    r = client.post(
        "/api/connections",
        json={"kind": kind, "id": ident, "settings": settings, "secrets": secrets, **kw},
    )
    assert r.status_code == 201, r.text


def test_overview_has_one_row_per_connection(client: TestClient) -> None:
    _add(client, "portainer", "home", {"url": "https://a.example:9443"}, {"api_key": "k"})
    _add(
        client,
        "portainer",
        "offsite",
        {"url": "https://b.example:9443"},
        {"api_key": "k"},
        enabled=False,
    )
    got = client.get("/api/infra/overview").json()
    rows = {r["id"]: r for r in got["connections"]}
    assert set(rows) == {"home", "offsite"}  # parents only: no environments, stacks or guests
    assert got["dry_run"] is True
    assert rows["home"]["state"] == "unchecked"  # dry-run contacts nothing
    assert rows["offsite"]["state"] == "off"
    assert rows["home"]["web_url"] == "https://a.example:9443"
    assert "secrets" not in rows["home"] and "api_key" not in str(rows)


def test_detail_for_portainer_lists_its_environments(client: TestClient) -> None:
    _add(client, "portainer", "home", {"url": "https://a.example:9443"}, {"api_key": "k"})
    d = client.get("/api/infra/connections/home").json()
    assert d["id"] == "home" and d["kind"] == "portainer"
    assert {"label": "Open the Portainer page", "href": "/portainer?instance=home"} in d["links"]
    envs = d["sections"]["environments"]
    assert envs and {"id", "name", "status", "running"} <= set(envs[0])


def test_detail_for_proxmox_lists_nodes_and_guest_counts(client: TestClient) -> None:
    _add(
        client, "proxmox", "pve",
        {"host": "pve.lan", "token_id": "a@b!c"}, {"token_secret": "s"},
    )  # fmt: skip
    with Session(get_engine()) as s:
        s.add(Node(name="eve1", always_on=True, address="10.0.0.11"))
        s.commit()
    d = client.get("/api/infra/connections/pve").json()
    assert [n["name"] for n in d["sections"]["nodes"]] == ["eve1"]
    assert set(d["sections"]["guests"]) == {"total", "running"}
    assert {"label": "Nodes & guests", "href": "/nodes"} in d["links"]


def test_detail_of_a_switched_off_or_unknown_connection(client: TestClient) -> None:
    _add(client, "portainer", "old", {"url": "https://a.example"}, {"api_key": "k"}, enabled=False)
    d = client.get("/api/infra/connections/old").json()
    assert d["state"] == "off" and d["sections"] == {}
    assert client.get("/api/infra/connections/nope").status_code == 404


def test_pages_and_redirects(client: TestClient) -> None:
    # Connectivity's overview and guests moved: to Settings > Connections and Nodes & guests.
    r = client.get("/infra", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/infra/connections"
    r = client.get("/infra/guests?node=eve1&vmid=101", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/nodes?q=101"
    assert client.get("/infra/connections").status_code == 200
    _add(client, "portainer", "home", {"url": "https://a.example"}, {"api_key": "k"})
    detail = client.get("/infra/connections/home")
    assert detail.status_code == 200 and '"home"' in detail.text
    assert 'href="/infra/connections"' in detail.text  # the breadcrumb back
    client.cookies.clear()
    r = client.get("/infra/connections/home", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]
    assert client.get("/api/infra/overview").status_code == 401


def test_the_flat_status_view_is_unchanged(client: TestClient) -> None:
    got = client.get("/api/infra/status").json()
    assert {"proxmox", "pbs"} <= {c["target"] for c in got["checks"]}
