"""Passkey auth: bootstrap rule, invites, login/logout, and that the
dashboard/backup/infra routers actually gate on a session now.

A real WebAuthn ceremony needs a hardware/platform authenticator, which
pytest doesn't have — so ``auth.verify_registration``/``verify_authentication``
are monkeypatched here the same way this repo already mocks adapters
(``conftest.py``'s ``_isolated_settings``, the adapter factory tests). Only
the verification step is faked; everything else (option generation, session
cookie, DB writes, the bootstrap/invite rules) runs for real.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import webauthn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.db import session as session_module
from orchestrator.db.models import PendingApproval
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import ApprovalKind, ApprovalStatus
from orchestrator.web import auth as auth_lib
from orchestrator.web.app import create_app


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


def _credential(raw_id: bytes) -> dict[str, Any]:
    b64 = webauthn.helpers.bytes_to_base64url(raw_id)
    return {"id": b64, "rawId": b64, "response": {"transports": ["internal"]}}


def _fake_verify_registration(monkeypatch: pytest.MonkeyPatch, credential_id: bytes) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"pub-" + credential_id,
            sign_count=0,
            aaguid="",
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)


def _fake_verify_authentication(monkeypatch: pytest.MonkeyPatch, new_sign_count: int = 1) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(new_sign_count=new_sign_count)

    monkeypatch.setattr(auth_lib, "verify_authentication", _fake)


def _register(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    username: str,
    credential_id: bytes,
    invite_token: str | None = None,
) -> Any:
    """Drive the full begin->complete ceremony. The bootstrap/invite check
    runs in BOTH steps, so an expected failure may come back from ``begin``
    already — that response is returned as-is rather than asserted on, so
    callers testing the rejection path see the real status code."""
    _fake_verify_registration(monkeypatch, credential_id)
    r = client.post(
        "/api/auth/register/begin", json={"username": username, "invite_token": invite_token}
    )
    if r.status_code != 200:
        return r
    return client.post(
        "/api/auth/register/complete",
        json={
            "username": username,
            "device_label": "test-device",
            "invite_token": invite_token,
            "credential": _credential(credential_id),
        },
    )


def _login(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, *, username: str, credential_id: bytes
) -> Any:
    _fake_verify_authentication(monkeypatch)
    r = client.post("/api/auth/login/begin", json={"username": username})
    assert r.status_code == 200, r.text
    return client.post("/api/auth/login/complete", json={"credential": _credential(credential_id)})


# ---------------------------------------------------------------------------
# Bootstrap + invite rules
# ---------------------------------------------------------------------------


def test_first_admin_registers_without_an_invite(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        r = _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "storm"


def test_second_admin_without_invite_is_rejected(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        assert (
            _register(client, monkeypatch, username="storm", credential_id=b"cred-1").status_code
            == 200
        )
        client.post("/api/auth/logout")
        r = _register(client, monkeypatch, username="second", credential_id=b"cred-2")
    assert r.status_code == 403


def test_duplicate_username_is_rejected(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        assert (
            _register(client, monkeypatch, username="storm", credential_id=b"cred-1").status_code
            == 200
        )
        r = _register(client, monkeypatch, username="storm", credential_id=b"cred-2")
    assert r.status_code == 409


def test_invite_allows_a_second_admin_once_then_is_consumed(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        assert (
            _register(client, monkeypatch, username="storm", credential_id=b"cred-1").status_code
            == 200
        )

        invite_resp = client.post("/api/auth/invites", json={"label": "for Jane"})
        assert invite_resp.status_code == 200, invite_resp.text
        token = invite_resp.json()["token"]

        client.post("/api/auth/logout")
        r = _register(
            client, monkeypatch, username="jane", credential_id=b"cred-2", invite_token=token
        )
        assert r.status_code == 200, r.text

        client.post("/api/auth/logout")
        r2 = _register(
            client, monkeypatch, username="third", credential_id=b"cred-3", invite_token=token
        )
    assert r2.status_code == 403


def test_expired_invite_is_rejected(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        assert (
            _register(client, monkeypatch, username="storm", credential_id=b"cred-1").status_code
            == 200
        )

        with Session(get_engine()) as session:
            invite = session.exec(
                select(PendingApproval).where(PendingApproval.kind == ApprovalKind.ADMIN_INVITE)
            ).one_or_none()
            if invite is None:
                invite = PendingApproval(
                    kind=ApprovalKind.ADMIN_INVITE,
                    token="expired-token",
                    requested_by="storm",
                    expires_at=auth_lib.utcnow() - timedelta(hours=1),
                )
            else:
                invite.expires_at = auth_lib.utcnow() - timedelta(hours=1)
            session.add(invite)
            session.commit()
            token = invite.token

        client.post("/api/auth/logout")
        r = _register(
            client, monkeypatch, username="jane", credential_id=b"cred-2", invite_token=token
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


def test_login_with_valid_credential_sets_session(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
        client.post("/api/auth/logout")

        r = _login(client, monkeypatch, username="storm", credential_id=b"cred-1")
        assert r.status_code == 200, r.text

        # Session actually works against a protected endpoint.
        protected = client.get("/api/registry/services")
    assert protected.status_code == 200


def test_login_with_unknown_credential_401s(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
        client.post("/api/auth/logout")

        r = _login(client, monkeypatch, username="storm", credential_id=b"never-registered")
    assert r.status_code == 401


def test_logout_clears_the_session(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
        assert client.get("/api/registry/services").status_code == 200

        client.post("/api/auth/logout")
        r = client.get("/api/registry/services")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Route protection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/api/registry/services", "/api/registry/status", "/api/runs", "/api/infra/status"]
)
def test_protected_endpoints_401_without_a_session(app: Any, path: str) -> None:
    with TestClient(app) as client:
        r = client.get(path)
    assert r.status_code == 401


def test_dashboard_page_redirects_when_not_logged_in(app: Any) -> None:
    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_dashboard_page_renders_when_logged_in(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
        r = client.get("/")
    assert r.status_code == 200
    assert "storm" in r.text


# ---------------------------------------------------------------------------
# Devices + invites listing
# ---------------------------------------------------------------------------


def test_add_device_and_list_credentials(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="storm", credential_id=b"cred-1")

        _fake_verify_registration(monkeypatch, b"cred-2")
        begin = client.post("/api/auth/credentials/add/begin")
        assert begin.status_code == 200, begin.text
        complete = client.post(
            "/api/auth/credentials/add/complete",
            json={"device_label": "second-device", "credential": _credential(b"cred-2")},
        )
        assert complete.status_code == 200, complete.text

        creds = client.get("/api/auth/credentials").json()
    labels = {c["label"] for c in creds}
    assert labels == {"test-device", "second-device"}


def test_invite_listing_reflects_consumption(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
        token = client.post("/api/auth/invites", json={"label": "for Jane"}).json()["token"]

        before = client.get("/api/auth/invites").json()
        assert before[0]["status"] == ApprovalStatus.PENDING.value

        client.post("/api/auth/logout")
        _register(client, monkeypatch, username="jane", credential_id=b"cred-2", invite_token=token)
        client.post("/api/auth/logout")
        _login(client, monkeypatch, username="storm", credential_id=b"cred-1")

        after = client.get("/api/auth/invites").json()
    assert after[0]["status"] == ApprovalStatus.CONSUMED.value
    assert after[0]["approved_by"] == "jane"


def test_the_passkey_this_browser_uses_is_marked(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
        first = {c["label"]: c["this_device"] for c in client.get("/api/auth/credentials").json()}
        assert first == {"test-device": True}

        _fake_verify_registration(monkeypatch, b"cred-2")
        client.post("/api/auth/credentials/add/begin")
        client.post(
            "/api/auth/credentials/add/complete",
            json={"device_label": "phone", "credential": _credential(b"cred-2")},
        )
        added = {c["label"]: c["this_device"] for c in client.get("/api/auth/credentials").json()}
        assert added == {"test-device": False, "phone": True}

        client.post("/api/auth/logout")
        _login(client, monkeypatch, username="storm", credential_id=b"cred-1")
        again = {c["label"]: c["this_device"] for c in client.get("/api/auth/credentials").json()}
        assert again == {"test-device": True, "phone": False}

        page = client.get("/admins").text
        assert '"this_device": true' in page and "/static/admins.js" in page


def test_an_open_invite_keeps_its_link_until_used(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        _register(client, monkeypatch, username="storm", credential_id=b"cred-1")
        made = client.post("/api/auth/invites", json={"label": "for Jane"}).json()
        (open_one,) = client.get("/api/auth/invites").json()
        assert open_one["url"] == made["url"]

        client.post("/api/auth/logout")
        _register(
            client,
            monkeypatch,
            username="jane",
            credential_id=b"cred-2",
            invite_token=made["token"],
        )
        (used,) = client.get("/api/auth/invites").json()
    assert used["status"] == ApprovalStatus.CONSUMED.value
    assert used["url"] is None
