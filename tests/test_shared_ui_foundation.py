"""Shared base.html/app.css/app.js foundation (see docs/roadmap.md).

Every template now extends ``base.html`` and gets ``app.css`` + ``app.js``
from it instead of duplicating its own ``<style>``/nav markup. There is no
JS test runner in this repo, so this file is the substitute the task called
for: it renders each page through the real app (not a static grep over the
template source) and asserts the shared assets are actually linked, the
static files are servable, and nothing regressed in the nav/markup every
other template test already pins down.

Auth is driven the same way as ``tests/test_auth.py``/``tests/test_proxy_hosts.py``:
the WebAuthn verification step is monkeypatched, everything else (session
cookie, require_admin) runs for real. The helpers below are copied rather
than imported, matching the existing convention in this test suite.
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


def _fake_verify_registration(monkeypatch: pytest.MonkeyPatch, credential_id: bytes) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"pub-" + credential_id,
            sign_count=0,
            aaguid="",
        )

    monkeypatch.setattr(auth_lib, "verify_registration", _fake)


def _login_as_first_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_verify_registration(monkeypatch, b"cred-1")
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
        _login_as_first_admin(c, monkeypatch)
        yield c


# Every page an admin can reach once logged in. (login.html is covered
# separately below since it's pre-auth.)
ADMIN_PAGES = [
    "/",
    "/backup-policies",
    "/proxy-hosts",
    "/traffic",
    "/activity",
    "/infra",
    "/infra/guests",
    "/infra/snapshots",
]

#: Every sidebar nav link, grouped the way base.html renders them — used to
#: assert every page carries the full, grouped sidebar (not just its own
#: link) and that nothing was dropped when the flat top-nav became this.
NAV_GROUPS = [
    ("Overview", [("/", "Dashboard")]),
    ("Networking", [("/proxy-hosts", "Proxy hosts"), ("/traffic", "Traffic")]),
    ("Backups", [("/backup-policies", "Backup policies")]),
    (
        "Monitoring",
        [
            ("/activity", "Activity"),
            ("/infra", "Connectivity"),
            ("/infra/guests", "Guests"),
            ("/infra/snapshots", "Snapshots"),
        ],
    ),
]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_link_shared_css_and_js(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert '<link rel="stylesheet" href="/static/app.css">' in r.text
    assert '<script src="/static/app.js"></script>' in r.text


def test_admin_pages_share_one_grouped_sidebar(client: TestClient) -> None:
    """Regression guard for the top-nav -> sidebar migration: every admin
    page still renders the full, grouped sidebar — nothing dropped from
    navigation — with the current page's own link marked "current" and no
    other link marked current."""
    for path, current_href in [
        ("/", '/" class="current"'),
        ("/backup-policies", '/backup-policies" class="current"'),
        ("/proxy-hosts", '/proxy-hosts" class="current"'),
        ("/traffic", '/traffic" class="current"'),
        ("/activity", '/activity" class="current"'),
        ("/infra", '/infra" class="current"'),
        ("/infra/guests", '/infra/guests" class="current"'),
        ("/infra/snapshots", '/infra/snapshots" class="current"'),
    ]:
        r = client.get(path)
        assert r.status_code == 200, r.text
        assert '<aside class="sidebar">' in r.text
        for heading, links in NAV_GROUPS:
            assert f'<div class="nav-heading">{heading}</div>' in r.text
            for href, label in links:
                assert f'<a href="{href}"' in r.text
                assert label in r.text
        assert r.text.count('class="current"') == 1
        assert current_href in r.text


def test_login_page_links_shared_css_and_js(app: Any) -> None:
    with TestClient(app) as client:
        r = client.get("/login")
        assert r.status_code == 200, r.text
        assert '<link rel="stylesheet" href="/static/app.css">' in r.text
        assert '<script src="/static/app.js"></script>' in r.text
        # No admin session yet - the shared nav/logout button must not render.
        assert "logout-btn" not in r.text
        # ...nor the admin sidebar itself (base.html's {% block header %} is
        # fully overridden on unauthenticated pages — see login.html).
        assert '<aside class="sidebar">' not in r.text
        assert 'class="has-sidebar"' not in r.text


def test_unauthenticated_pages_never_show_the_admin_sidebar(app: Any) -> None:
    """setup.html/maintenance.html override {% block header %} with their
    own plain <header>, not the sidebar — same contract as login.html, just
    exercised through the maintenance page instead (setup.html needs no
    extra fixture to reach, but the maintenance "unknown service" page
    needs neither a registered service nor a services.yaml, so it is the
    simplest unauthenticated page to exercise here)."""
    with TestClient(app) as client:
        r = client.get("/maintenance/does-not-exist")
        assert r.status_code == 200, r.text
        assert '<aside class="sidebar">' not in r.text
        assert 'class="has-sidebar"' not in r.text


def test_app_css_and_js_are_served(app: Any) -> None:
    with TestClient(app) as client:
        css = client.get("/static/app.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]

        js = client.get("/static/app.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]
        # The pieces every page's failure path now relies on.
        assert "function apiFetch" in js.text
        assert "function toast" in js.text


def test_app_js_defines_global_error_surfacing(app: Any) -> None:
    """apiFetch is the fix for the previously-silent-on-failure call sites
    (see docs/roadmap.md): it must show a toast for both a non-2xx response
    and a network-level failure, unless a caller opts out."""
    with TestClient(app) as client:
        js = client.get("/static/app.js").text
    assert "window.apiFetch" in js
    assert "window.toast" in js
    assert "suppressToast" in js
    # Parses the FastAPI HTTPException {"detail": ...} convention, plus the
    # {"message": ...}/{"error": ...} shapes a couple of routers also use.
    assert "data.detail" in js
    assert "data.message" in js
    assert "data.error" in js
