"""The /setup page: gating (disabled / fresh / already configured / admin
exists) and the submit path, which must run init's own checks and writes.

The cluster is faked the same way test_cli_setup.py fakes it — by swapping
``cli_setup.make_discovery`` — so nothing touches the network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import webauthn
from fastapi.testclient import TestClient
from ruamel.yaml import YAML
from sqlmodel import Session, select

from orchestrator import cli_setup
from orchestrator import config as config_module
from orchestrator.adapters.errors import AdapterAuthError
from orchestrator.adapters.proxmox.base import BackupStorage, ClusterNode, ClusterStatus, Guest
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import AuditResult, GuestKind
from orchestrator.setup.envfile import read_env
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app

PVE_SECRET = "pve-secret-value-0001"
PBS_SECRET = "pbs-secret-value-0002"

CLUSTER = ClusterStatus(
    nodes=[
        ClusterNode("steady", online=True, local=True, ip="10.0.0.11"),
        ClusterNode("burst", online=True, local=False, ip="10.0.0.12"),
    ],
    quorate=True,
    cluster_name="lab",
)
GUESTS = [
    Guest(node="steady", vmid=100, kind=GuestKind.VM, name="gateway", status="running"),
    Guest(node="burst", vmid=110, kind=GuestKind.VM, name="media", status="running"),
]


class FakeDiscovery:
    """Same shape as test_cli_setup.FakeDiscovery: what init's checks call."""

    def __init__(self) -> None:
        self.fail_pbs = False
        self.calls = 0

    async def proxmox(self, settings: Any) -> cli_setup.ProxmoxFindings:
        self.calls += 1
        return cli_setup.ProxmoxFindings(
            "8.2", CLUSTER, [BackupStorage("pbs-lab", "store1", "10.0.0.20")]
        )

    async def pbs(self, settings: Any) -> cli_setup.PbsFindings:
        self.calls += 1
        if self.fail_pbs:
            raise AdapterAuthError("pbs rejected the API token (403)", status_code=403, body="")
        return cli_setup.PbsFindings("3.2", ["store1"])

    async def datastore(self, settings: Any, name: str) -> None:
        self.calls += 1

    async def guests(self, settings: Any) -> tuple[list[Guest], ClusterStatus]:
        self.calls += 1
        return list(GUESTS), CLUSTER

    def tcp(self, host: str, port: int) -> str | None:
        return None


@pytest.fixture
def discovery(monkeypatch: pytest.MonkeyPatch) -> FakeDiscovery:
    fake = FakeDiscovery()
    monkeypatch.setattr(cli_setup, "make_discovery", lambda: fake)
    return fake


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    monkeypatch.setenv("SETUP_PROJECT_DIR", str(path))
    config_module.reset_settings_cache()
    return path


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _form(**overrides: str) -> dict[str, Any]:
    values = {
        "PROXMOX_HOST": "10.0.0.11",
        "PROXMOX_TOKEN_ID": "orch@pve!t",
        "PROXMOX_TOKEN_SECRET": PVE_SECRET,
        "PBS_HOST": "10.0.0.20",
        "PBS_TOKEN_ID": "orch@pbs!t",
        "PBS_TOKEN_SECRET": PBS_SECRET,
        "WEBAUTHN_RP_ID": "orch.lab.test",
        "WEBAUTHN_ORIGIN": "https://orch.lab.test",
        "ORCHESTRATOR_IMAGE": "ghcr.io/s-kroonen/homelab-orchestrator:sha-abc1234",
        "PROXMOX_VERIFY_TLS": "false",
        "DRY_RUN": "true",
        **overrides,
    }
    return {"values": values, "include": "all", "gateway": "gateway", "always_on": "steady"}


def _register_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """test_auth.py's ceremony, with only the WebAuthn verification faked."""
    raw = b"cred-1"
    b64 = webauthn.helpers.bytes_to_base64url(raw)

    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=raw, credential_public_key=b"pub", sign_count=0, aaguid=""
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)
    assert client.post("/api/auth/register/begin", json={"username": "storm"}).status_code == 200
    r = client.post(
        "/api/auth/register/complete",
        json={
            "username": "storm",
            "device_label": "t",
            "credential": {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}},
        },
    )
    assert r.status_code == 200, r.text


def _audits(action: str) -> list[AuditEntry]:
    with Session(get_engine()) as s:
        return list(s.exec(select(AuditEntry).where(AuditEntry.action == action)).all())


# ---------------------------------------------------------------------------
# gating
# ---------------------------------------------------------------------------


def test_setup_is_off_unless_a_project_dir_is_configured(app: Any) -> None:
    with TestClient(app) as client:
        assert client.get("/setup").status_code == 404
        assert client.post("/api/setup", json=_form()).status_code == 404


def test_fresh_install_with_no_admin_is_open(app: Any, project: Path) -> None:
    with TestClient(app) as client:
        r = client.get("/setup")

    assert r.status_code == 200
    assert 'name="PROXMOX_TOKEN_SECRET"' in r.text
    assert 'name="MQTT_HOST"' in r.text
    # Prefilled from the hostname the browser is already using.
    assert 'name="WEBAUTHN_RP_ID" value="testserver"' in r.text


def test_an_empty_env_file_still_counts_as_fresh(app: Any, project: Path) -> None:
    # What `touch .env` leaves — the same rule init applies.
    (project / ".env").write_text("\n", encoding="utf-8")
    with TestClient(app) as client:
        assert client.get("/setup").status_code == 200


@pytest.mark.parametrize("which", [".env", "config/services.yaml"])
def test_already_configured_is_404(app: Any, project: Path, which: str) -> None:
    path = project / which
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("PROXMOX_HOST=10.0.0.11\n", encoding="utf-8")
    with TestClient(app) as client:
        assert client.get("/setup").status_code == 404
        assert client.post("/api/setup", json=_form()).status_code == 404


def test_once_an_admin_exists_setup_needs_their_login(
    app: Any, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _register_admin(client, monkeypatch)
        assert client.get("/setup").status_code == 200  # logged in as that admin

        client.post("/api/auth/logout")
        r = client.get("/setup", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=/setup"
        assert client.post("/api/setup", json=_form()).status_code == 401


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


def test_submit_writes_env_and_registry_then_closes_the_page(
    app: Any, project: Path, discovery: FakeDiscovery
) -> None:
    with TestClient(app) as client:
        r = client.post("/api/setup", json=_form())
        assert r.status_code == 200, r.text
        # Post-setup: the gate is closed for good.
        assert client.get("/setup").status_code == 404
        assert client.post("/api/setup", json=_form()).status_code == 404

    data = r.json()
    assert data["status"] == "written"
    assert data["restart_required"] is True
    assert sorted(data["services"]) == ["gateway", "media"]
    assert discovery.calls > 0  # init's live checks ran, against the fake

    env = read_env(project / ".env")
    assert env["PROXMOX_TOKEN_SECRET"] == PVE_SECRET
    assert env["PBS_DATASTORE"] == "store1"  # chosen the way init chooses it
    assert env["PVE_BACKUP_STORAGE"] == "pbs-lab"  # worked out, not asked
    assert env["GATEWAY_SERVICE"] == "gateway"
    assert env["PROXMOX_VERIFY_TLS"] == "false"
    assert len(env["SESSION_SECRET"]) > 40

    registry = YAML(typ="safe").load((project / "config" / "services.yaml").read_text())
    assert {n["name"]: n["always_on"] for n in registry["nodes"]} == {
        "steady": True,
        "burst": False,
    }

    # Secrets never come back to the browser, not even in the captured log.
    assert PVE_SECRET not in r.text and PBS_SECRET not in r.text

    [entry] = _audits("setup.completed")
    assert entry.actor == "setup-wizard"  # no admin existed yet
    assert entry.details["services"] == 2
    assert PVE_SECRET not in str(entry.details)


def test_a_failed_check_writes_nothing(app: Any, project: Path, discovery: FakeDiscovery) -> None:
    discovery.fail_pbs = True
    with TestClient(app) as client:
        r = client.post("/api/setup", json=_form())
        # Still fresh, so the page stays available to fix the value and retry.
        assert client.get("/setup").status_code == 200

    assert r.status_code == 400
    assert "PBS API check failed" in r.json()["detail"]
    assert "FAIL  PBS API" in r.json()["log"]
    assert not (project / ".env").exists()
    assert not (project / "config" / "services.yaml").exists()
    [entry] = _audits("setup.failed")
    assert entry.result == AuditResult.ERROR


def test_no_verify_skips_the_cluster(app: Any, project: Path, discovery: FakeDiscovery) -> None:
    # Unchecked, nothing can be discovered — so, as with init --no-verify, the
    # datastore and storage ID must be given rather than worked out.
    unchecked = _form()
    unchecked["verify"] = False
    with TestClient(app) as client:
        r = client.post("/api/setup", json=unchecked)
    assert r.status_code == 400
    assert "PBS_DATASTORE has no value" in r.json()["detail"]

    body = _form(PBS_DATASTORE="store1", PVE_BACKUP_STORAGE="pbs-lab")
    body["verify"] = False
    body["gateway"] = ""  # no guests are listed unchecked, so none can be the gateway
    with TestClient(app) as client:
        r = client.post("/api/setup", json=body)

    assert r.status_code == 200, r.text
    assert discovery.calls == 0
    assert r.json()["services"] == []  # as with init --no-verify: no guests to add
    assert (project / ".env").is_file()


def test_unknown_and_missing_fields_are_refused(
    app: Any, project: Path, discovery: FakeDiscovery
) -> None:
    sneaky = _form(DATABASE_URL="sqlite:////tmp/evil.db")
    missing = _form(PROXMOX_TOKEN_SECRET="")
    with TestClient(app) as client:
        r1 = client.post("/api/setup", json=sneaky)
        r2 = client.post("/api/setup", json=missing)

    assert r1.status_code == 400
    assert "DATABASE_URL" in r1.json()["detail"]
    assert r2.status_code == 400
    assert "API token secret" in r2.json()["detail"]
    assert discovery.calls == 0
    assert not (project / ".env").exists()
