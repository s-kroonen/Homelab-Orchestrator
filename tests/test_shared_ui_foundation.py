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
from tests.auth_helpers import setup_token


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
    r = client.post(
        "/api/auth/register/begin", json={"username": "storm", "invite_token": setup_token()}
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/auth/register/complete",
        json={
            "username": "storm",
            "device_label": "test-device",
            "invite_token": setup_token(),
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
    "/services",
    "/admins",
    "/backup-policies",
    "/traffic",
    "/activity",
    "/sources",
    "/infra/snapshots",
]

#: The navigation of docs/design/README.md, grouped the way base.html renders it.
NAV_GROUPS = [
    ("Overview", [("/", "Dashboard"), ("/map", "Map")]),
    ("Services", [("/services", "Services"), ("/backup-policies", "Backups")]),
    ("Activity", [("/activity", "Activity"), ("/traffic", "Traffic")]),
    (
        "Settings",
        [
            ("/notifications", "Notifications"),
            ("/assistants", "AI assistants"),
            ("/admins", "Admins &amp; devices"),
            ("/system", "System"),
            ("/archive", "Archive"),
        ],
    ),
]


def _sidebar(html: str) -> str:
    start = html.index('<nav id="sidebar-nav"')
    return html[start : html.index("</nav>", start)]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_link_shared_css_and_js(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, r.text
    # Versioned by content, so browsers may keep them (see web/templating.py).
    assert '<link rel="stylesheet" href="/static/app.css?v=' in r.text
    for script in ("app.js", "ui.js", "shell.js", "live.js"):
        assert f'<script src="/static/{script}?v=' in r.text


def test_admin_pages_share_one_grouped_sidebar(client: TestClient) -> None:
    """Every admin page renders the full, grouped navigation, with exactly one entry marked
    current: the page's own, or the entry the page lives under."""
    for path, current_href in [
        ("/", "/"),
        ("/services", "/services"),
        ("/admins", "/admins"),
        ("/backup-policies", "/backup-policies"),
        ("/traffic", "/traffic"),
        ("/activity", "/activity"),
        # Every connection is a source (OR-78) ...
        ("/sources", "/sources"),
        # ... snapshots under Backups.
        ("/infra/snapshots", "/backup-policies"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, r.text
        assert '<aside class="sidebar">' in r.text
        nav = _sidebar(r.text)
        for heading, links in NAV_GROUPS:
            assert f'<div class="nav-heading">{heading}</div>' in nav
            for href, label in links:
                assert f'<a href="{href}"' in nav and label in nav
        assert '<div class="nav-heading">Sources' in nav and '<a href="/sources"' in nav
        assert nav.count('class="current"') == 1, path
        assert f'<a href="{current_href}" class="current"' in nav, path


def test_admin_pages_have_the_header(client: TestClient) -> None:
    html = client.get("/services").text
    for needle in ('id="global-search"', 'id="pending-pill"', 'id="live-pill"', 'id="user-btn"'):
        assert needle in html
    assert 'class="phonebar"' in html  # the bottom tab bar on a phone


def test_login_page_links_shared_css_and_js(app: Any) -> None:
    with TestClient(app) as client:
        r = client.get("/login")
        assert r.status_code == 200, r.text
        assert '<link rel="stylesheet" href="/static/app.css?v=' in r.text
        assert '<script src="/static/app.js?v=' in r.text
        # No admin session yet - neither the header nor the admin sidebar render.
        assert 'id="user-btn"' not in r.text
        assert '<aside class="sidebar">' not in r.text
        assert 'class="has-sidebar"' not in r.text


def test_versioned_static_files_are_cached_for_good(app: Any) -> None:
    from orchestrator.web.templating import asset

    with TestClient(app) as client:
        url = asset("app.css")
        assert url.startswith("/static/app.css?v=") and len(url) > len("/static/app.css?v=")
        r = client.get(url)
        assert r.status_code == 200
        assert "immutable" in r.headers["cache-control"]
        # Without a version: still revalidated every time.
        assert client.get("/static/app.css").headers["cache-control"] == "no-cache"


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


def _static(app: Any, name: str) -> str:
    with TestClient(app) as client:
        r = client.get(f"/static/{name}")
    assert r.status_code == 200
    return r.text


def test_app_js_counts_writes_in_flight_and_marks_the_page(app: Any) -> None:
    """OR-97: every non-GET apiFetch is counted (always released in a finally), exposed as
    window.OrchBusy, and mirrored as the ``api-busy`` class on <html>."""
    js = _static(app, "app.js")
    assert "window.OrchBusy" in js
    assert "writes: () => writes" in js
    assert "on(fn)" in js
    assert 'verb !== "GET" && verb !== "HEAD"' in js
    assert "finally" in js and "endWrite()" in js
    assert 'classList.toggle("api-busy", writes > 0)' in js


def test_app_js_shows_a_progress_bar_and_a_slow_write_toast(app: Any) -> None:
    js = _static(app, "app.js")
    assert 'id = "busy-bar"' in js
    assert "BAR_DELAY_MS = 150" in js
    assert "SLOW_MS = 2000" in js
    assert 'toast("Working…", "pending", { timeoutMs: 0 })' in js
    assert "dismissSlow()" in js


def test_app_js_quiet_writes_opt_out_of_feedback(app: Any) -> None:
    js = _static(app, "app.js")
    assert "!options.quiet" in js
    assert "opts.quiet" in js  # documented in the header


def test_app_css_styles_the_busy_bar_pending_toast_and_button_spinner(app: Any) -> None:
    css = _static(app, "app.css")
    assert "#busy-bar" in css
    assert "var(--accent)" in css.split("#busy-bar::after")[1].split("}")[0]
    assert ".toast.pending { border-left-color: var(--pending); }" in css
    assert 'html.api-busy [aria-busy="true"]' in css
    assert "cursor: progress" in css
    assert "#busy-bar::after { animation: none;" in css  # reduced motion: a static bar


def test_ui_el_marks_a_button_busy_while_its_click_returns_a_promise(app: Any) -> None:
    js = _static(app, "ui.js")
    assert 'typeof work.then !== "function"' in js
    assert 'setAttribute("aria-busy", "true")' in js
    assert "node.dataset.busy" in js
    assert "ev.stopImmediatePropagation()" in js
    assert 'k === "onclick"' in js
    # not `disabled`: that would drop the keyboard focus
    assert "node.disabled" not in js.split("function markBusy")[1].split("function busyClick")[0]


def test_ui_menu_marks_its_anchor_busy_while_an_item_runs(app: Any) -> None:
    js = _static(app, "ui.js")
    assert "markBusy(anchor, it.onClick())" in js


def test_hand_made_busy_wrapper_is_gone_from_service_js(app: Any) -> None:
    assert "const busy = (btn" not in _static(app, "service.js")
