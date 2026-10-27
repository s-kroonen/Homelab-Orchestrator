"""The Connections API: add / edit / test / default / delete / import, secrets never shown."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator import config as config_module
from orchestrator.config import get_settings
from orchestrator.connections.store import path_for
from orchestrator.db import session as session_module
from orchestrator.db.models import Node, Service
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


def _add(client: TestClient, ident: str, url: str, **kw: Any) -> Any:
    return client.post(
        "/api/connections",
        json={
            "kind": "portainer",
            "id": ident,
            "name": ident,
            "settings": {"url": url, "verify_tls": False},
            "secrets": {"api_key": f"key-{ident}"},
            **kw,
        },
    )


def test_needs_an_admin() -> None:
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        assert c.get("/api/connections").status_code == 401


def test_listing_describes_every_kind(client: TestClient) -> None:
    got = client.get("/api/connections").json()
    kinds = {k["kind"] for k in got["kinds"]}
    assert {"proxmox", "portainer", "proxy", "ntfy"} <= kinds
    assert got["exists"] is False and got["error"] == ""


def test_add_edit_and_secrets_stay_hidden(client: TestClient) -> None:
    r = _add(client, "home", "https://home.example:9443", site="home")
    assert r.status_code == 201, r.text
    row = next(c for c in r.json()["connections"] if c["id"] == "home")
    assert row["secrets_set"] == {"api_key": True}
    assert "key-home" not in r.text and "key-home" not in client.get("/api/connections").text
    assert path_for(get_settings()).stat().st_mode & 0o777 == 0o600
    assert get_settings().portainer_url == "https://home.example:9443"

    # blank secret keeps the stored one; a new url is applied without a restart
    r = client.put(
        "/api/connections/home",
        json={"settings": {"url": "https://other.example:9443"}, "secrets": {"api_key": ""}},
    )
    assert r.status_code == 200, r.text
    assert get_settings().portainer_url == "https://other.example:9443"
    assert get_settings().portainer_api_key.get_secret_value() == "key-home"

    r = client.put("/api/connections/home", json={"clear_secrets": ["api_key"]})
    assert r.status_code == 422  # enabled, so the key is required

    r = client.put("/api/connections/home", json={"enabled": False, "clear_secrets": ["api_key"]})
    assert r.status_code == 200
    assert next(c for c in r.json()["connections"] if c["id"] == "home")["enabled"] is False


def test_validation_and_duplicates(client: TestClient) -> None:
    assert _add(client, "home", "https://a.example").status_code == 201
    assert _add(client, "home", "https://b.example").status_code == 409
    assert _add(client, "Bad Id!", "https://b.example").status_code == 422
    bad = client.post("/api/connections", json={"kind": "nope"})
    assert bad.status_code == 422
    r = client.post("/api/connections", json={"kind": "portainer", "id": "x"})
    assert r.status_code == 422 and "required" in r.json()["detail"]
    # only one Proxmox may be on
    one = {"kind": "proxmox", "settings": {"host": "pve1", "token_id": "a@b!c"}}
    one["secrets"] = {"token_secret": "s"}
    assert client.post("/api/connections", json={**one, "id": "pve1"}).status_code == 201
    r = client.post("/api/connections", json={**one, "id": "pve2"})
    assert r.status_code == 422


def test_second_portainer_keeps_the_env_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PORTAINER_URL", "https://env.example:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "envkey")
    config_module.reset_settings_cache()
    ids = {c["id"]: c["source"] for c in client.get("/api/connections").json()["connections"]}
    assert ids == {"portainer": "env"}
    assert _add(client, "offsite", "https://off.example:9443").status_code == 201
    got = {c["id"]: c["source"] for c in client.get("/api/connections").json()["connections"]}
    assert got == {"portainer": "file", "offsite": "file"}


def test_default_pins_links_and_reports_dirty(client: TestClient) -> None:
    _add(client, "home", "https://a.example:9443", default=True)
    _add(client, "offsite", "https://b.example:9443")
    with Session(get_engine()) as s:
        node = Node(name="eve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        s.add(
            Service(
                slug="web",
                name="Web",
                node_id=node.id,
                controller={"type": "portainer", "stack": "web"},
            )
        )
        s.commit()
    r = client.post("/api/connections/offsite/default")
    assert r.status_code == 200, r.text
    assert r.json()["pinned"] == ["web"]
    got = client.get("/api/connections/controllers").json()
    assert got == [
        {
            "slug": "web",
            "name": "Web",
            "type": "portainer",
            "connection": "home",
            "connection_name": "home",
            "usable": True,
        }
    ]
    assert client.post("/api/connections/missing/default").status_code == 404


def test_delete_refuses_while_a_service_uses_it(client: TestClient) -> None:
    _add(client, "home", "https://a.example:9443")
    with Session(get_engine()) as s:
        node = Node(name="eve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        s.add(
            Service(
                slug="web",
                name="Web",
                node_id=node.id,
                controller={"type": "portainer", "stack": "web", "instance": "home"},
            )
        )
        s.commit()
    r = client.delete("/api/connections/home")
    assert r.status_code == 409 and "web" in r.json()["detail"]
    assert client.delete("/api/connections/home?force=true").status_code == 200
    assert client.delete("/api/connections/home").status_code == 404


def test_test_endpoints_honour_dry_run(client: TestClient) -> None:
    _add(client, "home", "https://a.example:9443")
    r = client.post("/api/connections/home/test")
    assert r.status_code == 200 and r.json()["ok"] is None and "dry-run" in r.json()["detail"]
    r = client.post(
        "/api/connections/test",
        json={"kind": "portainer", "existing_id": "home", "settings": {"url": "https://x.example"}},
    )
    assert r.status_code == 200 and r.json()["ok"] is None
    r = client.post("/api/connections/test", json={"kind": "portainer", "settings": {}})
    assert r.status_code == 200 and r.json()["ok"] is False  # url missing


def test_import_env_moves_and_lists_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PORTAINER_URL", "https://env.example:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "envkey")
    config_module.reset_settings_cache()
    r = client.post("/api/connections/import-env")
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == ["portainer"]
    assert "PORTAINER_URL" in r.json()["delete_from_env"]
    assert client.post("/api/connections/import-env").json()["moved"] == []


def test_shared_bundle_round_trip(client: TestClient) -> None:
    _add(client, "offsite", "https://off.example:9443", scope="shared", site="offsite")
    _add(client, "home", "https://home.example:9443")
    body = client.get("/api/connections/export").text
    assert "offsite" in body and "home" not in body and "key-offsite" not in body
    full = client.get("/api/connections/export?with_secrets=true").text
    assert "key-offsite" in full

    assert client.delete("/api/connections/offsite").status_code == 200
    r = client.post("/api/connections/import", json={"body": body})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == ["offsite"]
    row = next(c for c in r.json()["connections"] if c["id"] == "offsite")
    assert row["enabled"] is False and row["scope"] == "shared"  # secrets still to be added
    assert client.post("/api/connections/import", json={"body": "version: 9"}).status_code == 422


def test_a_broken_file_is_reported_not_fatal(client: TestClient) -> None:
    path = path_for(get_settings())
    path.write_text("version: 1\nconnections: [oops\n")
    r = client.post("/api/connections/reload")
    assert r.status_code == 200 and r.json()["error"]
    assert client.get("/api/connections").json()["error"]


def test_the_watcher_applies_an_edit_made_outside(client: TestClient) -> None:
    import asyncio

    from orchestrator.connections.watch import ConnectionsWatcher

    _add(client, "home", "https://a.example:9443")
    watcher = ConnectionsWatcher(client.app.state.adapters)  # type: ignore[attr-defined]
    assert asyncio.run(watcher.check_once()) is False  # our own write is already applied
    path = path_for(get_settings())
    path.write_text(path.read_text().replace("a.example", "b.example"))
    assert asyncio.run(watcher.check_once()) is True
    assert get_settings().portainer_url == "https://b.example:9443"
    assert asyncio.run(watcher.check_once()) is False


def test_cli_adds_lists_and_hides_secrets(client: TestClient) -> None:
    from click.testing import CliRunner

    from orchestrator.cli import cli

    run = CliRunner()
    out = run.invoke(
        cli,
        ["connections", "add", "portainer", "--id", "offsite", "--shared", "--site", "offsite",
         "--set", "url=https://off.example:9443", "--set", "api_key=topsecret"],
    )  # fmt: skip
    assert out.exit_code == 0, out.output
    out = run.invoke(cli, ["connections", "list"])
    assert out.exit_code == 0 and "offsite" in out.output and "shared" in out.output
    shown = run.invoke(cli, ["connections", "show", "offsite"])
    assert "topsecret" not in shown.output and '"api_key": true' in shown.output
    bad = run.invoke(cli, ["connections", "add", "portainer", "--set", "nope=1"])
    assert bad.exit_code != 0 and "no field" in bad.output
    out = run.invoke(cli, ["connections", "remove", "offsite", "--yes"])
    assert out.exit_code == 0, out.output


def test_the_page_renders_for_admins_only(client: TestClient) -> None:
    page = client.get("/connections")
    assert page.status_code == 200 and "Connections" in page.text and 'id="c-kinds"' in page.text
    client.cookies.clear()
    r = client.get("/connections", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]
