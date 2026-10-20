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
    assert '<link rel="stylesheet" href="/static/app.css">' in r.text
    assert '<script src="/static/app.js"></script>' in r.text
    assert '<aside class="sidebar">' in r.text


def test_connectivity_page_fetches_infra_status(client: TestClient) -> None:
    r = client.get("/infra")
    assert r.status_code == 200, r.text
    assert "/api/infra/status" in r.text
    assert "orchestrator-cli check" in r.text


def test_guests_page_fetches_infra_guests(client: TestClient) -> None:
    r = client.get("/infra/guests")
    assert r.status_code == 200, r.text
    assert "/api/infra/proxmox/guests" in r.text
    assert "orchestrator-cli guests" in r.text


def test_snapshots_page_fetches_infra_snapshots(client: TestClient) -> None:
    r = client.get("/infra/snapshots")
    assert r.status_code == 200, r.text
    assert "/api/infra/pbs/snapshots" in r.text
    assert "orchestrator-cli snapshots" in r.text


# --- real data through a mocked adapter, same monkeypatch pattern as
# tests/test_guest_power_api.py --------------------------------------------


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
