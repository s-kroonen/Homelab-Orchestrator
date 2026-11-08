"""OR-56: rename and remove passkeys, disable / enable / remove an admin, revoke an invite."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.cli import cli
from orchestrator.db import session as session_module
from orchestrator.db.models import Admin, AuditEntry, WebAuthnCredential
from orchestrator.db.session import get_engine
from orchestrator.web.app import create_app
from tests.test_auth import _credential, _fake_verify_registration, _login, _register

ME = "/api/auth/credentials"


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _add_device(client: TestClient, monkeypatch: pytest.MonkeyPatch, raw: bytes, label: str) -> int:
    _fake_verify_registration(monkeypatch, raw)
    client.post("/api/auth/credentials/add/begin")
    r = client.post(
        "/api/auth/credentials/add/complete",
        json={"device_label": label, "credential": _credential(raw)},
    )
    assert r.status_code == 200, r.text
    return next(c["id"] for c in client.get(ME).json() if c["label"] == label)


def _two_admins(client: TestClient, other: TestClient, monkeypatch: pytest.MonkeyPatch) -> int:
    """admin (on ``client``) and second (on ``other``), both signed in; returns second's id."""
    assert _register(client, monkeypatch, username="admin", credential_id=b"a1").status_code == 200
    token = client.post("/api/auth/invites", json={}).json()["token"]
    r = _register(other, monkeypatch, username="second", credential_id=b"s1", invite_token=token)
    assert r.status_code == 200
    return next(a["id"] for a in client.get("/api/auth/admins").json() if a["username"] == "second")


def _actions() -> list[str]:
    with Session(get_engine()) as s:
        return [e.action for e in s.exec(select(AuditEntry)).all()]


# -- passkeys ------------------------------------------------------------------------------------


def test_a_passkey_can_be_renamed(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="admin", credential_id=b"c1")
        (cred,) = client.get(ME).json()
        r = client.patch(f"{ME}/{cred['id']}", json={"label": "  Laptop  "})
        assert r.status_code == 200
        assert client.get(ME).json()[0]["label"] == "Laptop"
        assert client.patch(f"{ME}/{cred['id']}", json={"label": " "}).status_code == 400
    assert "auth.credential.renamed" in _actions()


def test_your_only_passkey_cannot_be_removed(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="admin", credential_id=b"c1")
        (cred,) = client.get(ME).json()
        r = client.delete(f"{ME}/{cred['id']}")
        assert r.status_code == 409 and "only passkey" in r.json()["detail"]


def test_a_removed_passkey_cannot_sign_in_and_its_sessions_end(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as laptop, TestClient(app) as phone:
        _register(laptop, monkeypatch, username="admin", credential_id=b"c1")
        _login(phone, monkeypatch, username="admin", credential_id=b"c1")
        stolen = _add_device(phone, monkeypatch, b"c2", "phone")

        r = laptop.delete(f"{ME}/{stolen}")
        assert r.status_code == 200 and r.json()["signed_out"] is False
        assert phone.get(ME).status_code == 401
        assert _login(phone, monkeypatch, username="admin", credential_id=b"c2").status_code == 401
        assert laptop.get(ME).status_code == 200
    assert "auth.credential.removed" in _actions()


def test_removing_this_devices_passkey_signs_this_browser_out(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="admin", credential_id=b"c1")
        this = _add_device(client, monkeypatch, b"c2", "here")
        r = client.delete(f"{ME}/{this}")
        assert r.json()["signed_out"] is True
        assert client.get(ME).status_code == 401
        assert _login(client, monkeypatch, username="admin", credential_id=b"c1").status_code == 200


def test_another_admins_passkey_is_out_of_reach(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client, TestClient(app) as other:
        _two_admins(client, other, monkeypatch)
        (theirs,) = other.get(ME).json()
        assert client.delete(f"{ME}/{theirs['id']}").status_code == 404
        assert client.patch(f"{ME}/{theirs['id']}", json={"label": "x"}).status_code == 404


# -- admins --------------------------------------------------------------------------------------


def test_disabling_an_admin_ends_their_sessions_even_after_enabling(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client, TestClient(app) as other:
        second = _two_admins(client, other, monkeypatch)
        assert other.get(ME).status_code == 200

        assert client.post(f"/api/auth/admins/{second}/disable").status_code == 200
        assert other.get(ME).status_code == 401
        assert _login(other, monkeypatch, username="second", credential_id=b"s1").status_code == 401
        listed = {a["username"]: a for a in client.get("/api/auth/admins").json()}
        assert listed["second"]["disabled_at"] and listed["admin"]["you"]

        assert client.post(f"/api/auth/admins/{second}/enable").status_code == 200
        assert other.get(ME).status_code == 401  # the old session stays ended
        assert _login(other, monkeypatch, username="second", credential_id=b"s1").status_code == 200
        assert other.get(ME).status_code == 200
    assert {"auth.admin.disabled", "auth.admin.enabled"} <= set(_actions())


def test_you_cannot_disable_or_remove_yourself(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="admin", credential_id=b"c1")
        (me,) = client.get("/api/auth/admins").json()
        assert client.post(f"/api/auth/admins/{me['id']}/disable").status_code == 409
        assert client.delete(f"/api/auth/admins/{me['id']}").status_code == 409


def test_removing_an_admin_removes_their_passkeys_and_sessions(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client, TestClient(app) as other:
        second = _two_admins(client, other, monkeypatch)
        assert client.delete(f"/api/auth/admins/{second}").status_code == 200
        assert other.get(ME).status_code == 401
        assert [a["username"] for a in client.get("/api/auth/admins").json()] == ["admin"]
    with Session(get_engine()) as s:
        assert len(s.exec(select(WebAuthnCredential)).all()) == 1
    assert "auth.admin.removed" in _actions()


# -- invites -------------------------------------------------------------------------------------


def test_a_revoked_invite_no_longer_registers(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client, TestClient(app) as anon:
        _register(client, monkeypatch, username="admin", credential_id=b"c1")
        token = client.post("/api/auth/invites", json={"label": "friend"}).json()["token"]
        invite = next(i for i in client.get("/api/auth/invites").json() if i["label"] == "friend")
        assert client.delete(f"/api/auth/invites/{invite['id']}").status_code == 200

        listed = next(i for i in client.get("/api/auth/invites").json() if i["id"] == invite["id"])
        assert listed["status"] == "denied" and listed["url"] is None
        r = _register(anon, monkeypatch, username="friend", credential_id=b"f1", invite_token=token)
        assert r.status_code == 403
    assert "auth.invite.revoked" in _actions()


# -- CLI -----------------------------------------------------------------------------------------


def test_the_cli_disables_and_enables_and_keeps_one_admin_active(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client, TestClient(app) as other:
        _two_admins(client, other, monkeypatch)
        runner = CliRunner()
        out = runner.invoke(cli, ["admin", "list"]).output
        assert "second" in out and "active" in out

        assert runner.invoke(cli, ["admin", "disable", "second"]).exit_code == 0
        assert other.get(ME).status_code == 401
        last = runner.invoke(cli, ["admin", "disable", "admin"])
        assert last.exit_code != 0 and "only active admin" in last.output
        assert runner.invoke(cli, ["admin", "enable", "second"]).exit_code == 0
    with Session(get_engine()) as s:
        assert all(a.disabled_at is None for a in s.exec(select(Admin)).all())
