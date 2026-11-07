"""OR-58: no open first-admin signup, and the host-side ways back in.

* Registration needs an invite also while no admin exists: the setup link.
* ``admin reset`` ends every session and invite and prints a setup link.
* ``admin invite`` prints a link without removing anything (``--for`` adds a passkey).
* ``admin passkeys`` lists passkeys and removes one, ending the sessions it signed in.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.cli import cli
from orchestrator.db import session as session_module
from orchestrator.db.models import Admin, PendingApproval, WebAuthnCredential
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import ApprovalKind
from orchestrator.web.app import create_app
from tests.auth_helpers import setup_token
from tests.test_auth import _credential, _fake_verify_registration, _login, _register

ME = "/api/auth/credentials"  # any signed-in-only endpoint


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _link_token(output: str) -> str:
    match = re.search(r"/login\?invite=([\w-]+)", output)
    assert match, output
    return match.group(1)


def _cli(*args: str) -> str:
    result = CliRunner().invoke(cli, ["admin", *args])
    assert result.exit_code == 0, result.output
    return result.output


# -- a fresh install -----------------------------------------------------------------------------


def test_nobody_registers_the_first_admin_without_the_setup_link(app: Any) -> None:
    with TestClient(app) as anon:
        r = anon.post("/api/auth/register/begin", json={"username": "intruder"})
        assert r.status_code == 403
        assert "setup link" in r.json()["detail"]
        page = anon.get("/login").text
        assert "No admin yet" in page and 'id="register-btn"' not in page


def test_the_start_makes_one_setup_link_and_reuses_it(app: Any) -> None:
    with TestClient(app):
        pass
    with TestClient(app):  # a restart
        pass
    with Session(get_engine()) as s:
        rows = s.exec(select(PendingApproval)).all()
    assert len(rows) == 1 and rows[0].payload["setup"] is True


def test_the_setup_link_registers_the_first_admin_once(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        token = setup_token()
        page = client.get(f"/login?invite={token}").text
        assert "Create the first admin" in page
        r = _register(
            client, monkeypatch, username="admin", credential_id=b"c1", invite_token=token
        )
        assert r.status_code == 200
        client.post("/api/auth/logout")
        again = _register(
            client, monkeypatch, username="other", credential_id=b"c2", invite_token=token
        )
    assert again.status_code == 403


def test_a_second_setup_link_dies_once_the_first_admin_exists(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        in_the_log = setup_token()
        from_the_host = _link_token(_cli("invite"))
        assert from_the_host != in_the_log
        r = _register(
            client, monkeypatch, username="admin", credential_id=b"c1", invite_token=from_the_host
        )
        assert r.status_code == 200
        client.post("/api/auth/logout")
        late = _register(
            client, monkeypatch, username="late", credential_id=b"c2", invite_token=in_the_log
        )
    assert late.status_code == 403


# -- admin reset ---------------------------------------------------------------------------------


def test_reset_ends_every_session_and_invite_and_prints_a_setup_link(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as old, TestClient(app) as anon:
        assert _register(old, monkeypatch, username="admin", credential_id=b"c1").status_code == 200
        handed_out = old.post("/api/auth/invites", json={"label": "for a friend"}).json()["token"]

        out = _cli("reset", "--yes")
        token = _link_token(out)

        # The old cookie names admin 1 and passkey 1; the new first admin will get both ids
        # again, so only the passkey hash tells them apart.
        assert old.get(ME).status_code == 401
        assert anon.post("/api/auth/register/begin", json={"username": "x"}).status_code == 403
        stale = _register(
            anon, monkeypatch, username="x", credential_id=b"c9", invite_token=handed_out
        )
        assert stale.status_code == 403

        r = _register(anon, monkeypatch, username="new", credential_id=b"c2", invite_token=token)
        assert r.status_code == 200
        assert anon.get(ME).status_code == 200
        assert old.get(ME).status_code == 401
    with Session(get_engine()) as s:
        (admin,) = s.exec(select(Admin)).all()
    assert admin.username == "new"


def test_reset_with_nobody_registered_changes_nothing() -> None:
    session_module.create_all_for_tests()
    out = _cli("reset", "--yes")
    assert "nothing to reset" in out and "invite=" not in out


# -- admin invite --------------------------------------------------------------------------------


def test_invite_for_an_admin_adds_a_passkey_to_that_account(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="admin", credential_id=b"c1")
        client.post("/api/auth/logout")

        token = _link_token(_cli("invite", "--for", "admin"))
        assert "Add a passkey to admin" in client.get(f"/login?invite={token}").text
        # The name typed does not matter: the link is for this account.
        r = _register(
            client, monkeypatch, username="ignored", credential_id=b"c2", invite_token=token
        )
        assert r.status_code == 200 and r.json()["username"] == "admin"
        assert [c["this_device"] for c in client.get(ME).json()] == [True, False]
    with Session(get_engine()) as s:
        assert len(s.exec(select(Admin)).all()) == 1
        assert len(s.exec(select(WebAuthnCredential)).all()) == 2


def test_invite_without_for_makes_a_new_admin(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="admin", credential_id=b"c1")
        client.post("/api/auth/logout")
        token = _link_token(_cli("invite", "--label", "colleague"))
        r = _register(
            client, monkeypatch, username="second", credential_id=b"c2", invite_token=token
        )
    assert r.status_code == 200 and r.json()["username"] == "second"


def test_invite_with_no_admin_prints_the_setup_link() -> None:
    session_module.create_all_for_tests()
    out = _cli("invite")
    assert "first admin" in out
    with Session(get_engine()) as s:
        (invite,) = s.exec(
            select(PendingApproval).where(PendingApproval.kind == ApprovalKind.ADMIN_INVITE)
        ).all()
    assert invite.payload["setup"] is True and invite.token == _link_token(out)


def test_invite_for_an_unknown_admin_fails() -> None:
    session_module.create_all_for_tests()
    result = CliRunner().invoke(cli, ["admin", "invite", "--for", "nobody"])
    assert result.exit_code != 0 and "no admin named" in result.output


# -- admin passkeys ------------------------------------------------------------------------------


def test_removing_a_passkey_ends_the_sessions_it_signed_in(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as laptop, TestClient(app) as phone:
        _register(laptop, monkeypatch, username="admin", credential_id=b"c1")
        # The phone signs in with the laptop's passkey once, then adds its own.
        _login(phone, monkeypatch, username="admin", credential_id=b"c1")
        _fake_verify_registration(monkeypatch, b"c2")
        phone.post("/api/auth/credentials/add/begin")
        phone.post(
            "/api/auth/credentials/add/complete",
            json={"device_label": "phone", "credential": _credential(b"c2")},
        )
        assert phone.get(ME).status_code == 200

        listing = _cli("passkeys")
        assert "phone" in listing and "test-device" in listing
        with Session(get_engine()) as s:
            stolen = s.exec(
                select(WebAuthnCredential).where(WebAuthnCredential.label == "phone")
            ).one()
        out = _cli("passkeys", "--remove", str(stolen.id), "--yes")
        assert "Removed" in out and "no passkey left" not in out

        assert phone.get(ME).status_code == 401
        assert laptop.get(ME).status_code == 200


def test_removing_the_last_passkey_says_how_back_in(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="admin", credential_id=b"c1")
    with Session(get_engine()) as s:
        cred = s.exec(select(WebAuthnCredential)).one()
    out = _cli("passkeys", "--remove", str(cred.id), "--yes")
    assert "admin invite --for admin" in out
