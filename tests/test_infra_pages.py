"""``/infra``, ``/infra/guests``, ``/infra/snapshots`` — the dashboard
equivalents of ``orchestrator-cli check``/``guests``/``snapshots`` (see
``src/orchestrator/web/routers/infra.py`` and ``src/orchestrator/cli.py``).

These pages are entirely client-rendered (the server only serves the shell;
the table is filled by ``apiFetch`` calls to the existing ``/api/infra/*``
JSON endpoints), so what matters here is: the page renders through the real
app, links the shared chrome, and is reachable once logged in — same
contract the other list-only pages already have. ``tests/test_node_power_api.py``
and ``tests/test_adapter_selection.py`` exercise the JSON endpoints' own
behavior; this file does not re-test those.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import webauthn
from fastapi.testclient import TestClient

from orchestrator.db import session as session_module
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _credential(raw_id: bytes) -> dict[str, Any]:
    b64 = webauthn.helpers.bytes_to_base64url(raw_id)
    return {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}}


def _login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=b"cred-1", credential_public_key=b"pub-cred-1", sign_count=0, aaguid=""
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)
    r = client.post("/api/auth/register/begin", json={"username": "storm", "invite_token": None})
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/auth/register/complete",
        json={
            "username": "storm",
            "device_label": "test-device",
            "invite_token": None,
            "credential": _credential(b"cred-1"),
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def client(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(app) as c:
        _login(c, monkeypatch)
        yield c


@pytest.mark.parametrize("path", ["/infra", "/infra/guests", "/infra/snapshots"])
def test_pages_require_admin(app: Any, path: str) -> None:
    with TestClient(app) as client:
        r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", ["/infra", "/infra/guests", "/infra/snapshots"])
def test_pages_render_shared_chrome(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert '<link rel="stylesheet" href="/static/app.css?v=' in r.text
    assert '<script src="/static/app.js?v=' in r.text
    assert '<aside class="sidebar">' in r.text


def test_connections_page_checks_every_connection(client: TestClient) -> None:
    r = client.get("/infra", follow_redirects=False)
    assert r.headers["location"] == "/infra/connections"
    assert '"/api/infra/overview"' in client.get("/static/connections.js").text


def test_guests_are_on_nodes_and_guests(client: TestClient) -> None:
    r = client.get("/infra/guests", follow_redirects=False)
    assert r.headers["location"] == "/nodes"
    assert '"/api/infra/proxmox/guests"' in client.get("/static/nodes.js").text


def test_snapshots_page_is_a_tab_of_backups(client: TestClient) -> None:
    r = client.get("/infra/snapshots", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/backup-policies?tab=snapshots"
    assert '"/api/infra/pbs/snapshots"' in client.get("/static/backups.js").text
    ds = client.get("/api/infra/pbs/datastore").json()
    assert set(ds) >= {"name", "reachable"}


def test_infra_status_api_shows_real_adapter_data(client: TestClient) -> None:
    """Sanity check that the API endpoint these pages call actually surfaces
    a mocked adapter's data — the page itself does nothing but display this
    JSON (see the inline <script> in infra_status.html)."""
    from orchestrator.adapters.proxmox.base import VersionInfo

    async def fake_version() -> VersionInfo:
        return VersionInfo(version="8.2.1", release="bookworm")

    client.app.state.adapters.proxmox.version = fake_version  # type: ignore[attr-defined]
    r = client.get("/api/infra/status")
    assert r.status_code == 200, r.text
    body = r.json()
    pve = next(c for c in body["checks"] if c["target"] == "proxmox")
    assert pve["ok"] is True
    assert pve["result"] == {"version": "8.2.1", "release": "bookworm"}


def test_infra_guests_api_shows_real_adapter_data(client: TestClient) -> None:
    from orchestrator.adapters.proxmox.base import Guest
    from orchestrator.domain.enums import GuestKind

    async def fake_list_guests(node: str | None = None) -> list[Guest]:
        return [Guest(node="eve1", vmid=9001, kind=GuestKind.VM, name="media", status="running")]

    client.app.state.adapters.proxmox.list_guests = fake_list_guests  # type: ignore[attr-defined]
    r = client.get("/api/infra/proxmox/guests")
    assert r.status_code == 200, r.text
    assert r.json() == [
        {
            "node": "eve1",
            "vmid": 9001,
            "kind": "vm",
            "name": "media",
            "status": "running",
            # Not registered as a service here.
            "service_slug": None,
            # Its node is not registered, so its boot settings were never read.
            "boot": None,
        }
    ]


def test_infra_snapshots_api_shows_real_adapter_data(client: TestClient) -> None:
    from orchestrator.adapters.pbs.base import Snapshot

    async def fake_list_snapshots(
        datastore: str, *, backup_type: str | None = None, backup_id: str | None = None
    ) -> list[Snapshot]:
        return [
            Snapshot(
                datastore=datastore,
                backup_type="vm",
                backup_id="9001",
                backup_time=1_700_000_000,
                size_bytes=123456,
                verified=True,
                protected=False,
                owner="orchestrator@pbs",
            )
        ]

    client.app.state.adapters.pbs.list_snapshots = fake_list_snapshots  # type: ignore[attr-defined]
    r = client.get("/api/infra/pbs/snapshots")
    assert r.status_code == 200, r.text
    (snap,) = r.json()
    assert snap["snapshot_id"] == "vm/9001/2023-11-14T22:13:20Z"
    assert snap["size_bytes"] == 123456
    assert snap["verified"] is True
    assert snap["owner"] == "orchestrator@pbs"


def test_guest_boot_settings_are_read_shown_and_changed(client: TestClient) -> None:
    """Nodes & guests: each guest's Start at boot / order is read from Proxmox while its node is
    on, kept, shown with its place in the boot sequence, and can be changed (in Proxmox)."""
    from orchestrator.adapters.proxmox.base import Guest, GuestBoot
    from orchestrator.domain.enums import GuestKind

    r = client.post("/api/registry/nodes", json={"name": "node-a", "power_mgr_target": "node-a"})
    assert r.status_code in (200, 201), r.text
    proxmox = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    proxmox.simulated_guests = [
        Guest(node="node-a", vmid=101, kind=GuestKind.VM, name="db", status="running"),
        Guest(node="node-a", vmid=102, kind=GuestKind.CT, name="app", status="stopped"),
    ]
    proxmox.simulated_boot[101] = GuestBoot(onboot=True, order=1, up_s=30)
    proxmox.simulated_onboot_delay["node-a"] = 10

    rows = {g["vmid"]: g for g in client.get("/api/infra/proxmox/guests").json()}
    assert rows[101]["boot"]["onboot"] is True and rows[101]["boot"]["position"] == 1
    assert rows[101]["boot"]["starts_after_s"] == 10
    assert rows[102]["boot"]["onboot"] is False and rows[102]["boot"]["position"] is None

    r = client.put("/api/infra/guests/node-a/102/boot", json={"onboot": True, "order": 2})
    assert r.status_code == 200, r.text
    assert proxmox.simulated_boot[102].onboot is True and proxmox.simulated_boot[102].order == 2
    rows = {g["vmid"]: g for g in client.get("/api/infra/proxmox/guests").json()}
    assert rows[102]["boot"]["position"] == 2

    assert client.put("/api/infra/guests/node-a/999/boot", json={"onboot": True}).status_code == 404
    bad = client.put("/api/infra/guests/node-a/101/boot", json={"onboot": True, "up_s": -1})
    assert bad.status_code == 400
