"""OR-5, OR-6, OR-8: no public wake, no cross-site actions, no public API map."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator import config as config_module
from orchestrator.db import session as session_module
from orchestrator.web.app import create_app
from orchestrator.web.same_origin import _host, verdict
from tests.test_proxy_hosts import _login_as_first_admin


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


@pytest.fixture
def admin(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(app) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


# -- OR-5: wake ----------------------------------------------------------------------------------


def test_a_visitor_cannot_wake_anything(app: Any) -> None:
    with TestClient(app) as anon:
        assert anon.post("/wake/example-media").status_code == 401
        assert anon.get("/wake/example-media").status_code == 401
        # The same answer whether or not the service exists: nothing to enumerate.
        assert anon.post("/wake/no-such-service").status_code == 401


def test_an_admin_still_can(admin: TestClient) -> None:
    r = admin.post("/wake/no-such-service")
    assert r.status_code == 404  # past the login check, to the pipeline's own answer


# -- OR-8: API docs ------------------------------------------------------------------------------


def test_the_api_map_is_not_public(app: Any) -> None:
    with TestClient(app) as anon:
        assert anon.get("/openapi.json").status_code == 404
        assert anon.get("/redoc").status_code == 404
        assert anon.get("/api/openapi.json").status_code == 401
        r = anon.get("/docs", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_an_admin_gets_the_reference(admin: TestClient) -> None:
    schema = admin.get("/api/openapi.json").json()
    assert "/api/services/{slug}/deploy" in schema["paths"]
    docs = admin.get("/docs")
    assert docs.status_code == 200 and "/api/openapi.json" in docs.text


# -- OR-6: cross-site requests -------------------------------------------------------------------

ACTION = "/api/system/network/check"  # a bare POST: no body needed, like the ones at risk


def test_a_post_from_another_site_is_refused(admin: TestClient) -> None:
    for headers in (
        {"Origin": "https://bingo.example.lan"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site"},  # a sibling subdomain, no Origin
        {"Sec-Fetch-Site": "cross-site"},
    ):
        r = admin.post(ACTION, headers=headers)
        assert r.status_code == 403, headers
        assert "another site" in r.json()["detail"]


def test_the_dashboard_itself_and_non_browsers_pass(admin: TestClient) -> None:
    assert admin.post(ACTION, headers={"Origin": "http://testserver"}).status_code == 200
    assert admin.post(ACTION, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert admin.post(ACTION).status_code == 200  # the CLI, curl: no browser headers at all
    assert admin.get("/api/system/network", headers={"Origin": "https://evil.example"}).is_success


def test_the_configured_address_counts_as_the_dashboard(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Behind a proxy that rewrites Host, the browser's Origin is the public address.
    monkeypatch.setenv("EXTERNAL_URL", "https://orchestrator.example.lan")
    config_module.reset_settings_cache()
    r = admin.post(ACTION, headers={"Origin": "https://orchestrator.example.lan"})
    assert r.status_code == 200


def test_token_authenticated_paths_are_left_to_their_token(admin: TestClient) -> None:
    foreign = {"Origin": "https://ci.example.org"}
    assert admin.post("/hooks/deploy/no-such-service", headers=foreign).status_code == 404
    assert admin.post("/mcp", headers=foreign, json={}).status_code == 401


def test_hosts_are_compared_without_default_ports() -> None:
    assert _host("https://Orch.Example.lan:443") == "orch.example.lan"
    assert _host("http://10.0.0.5:8080") == "10.0.0.5:8080"
    assert _host("10.0.0.5:8080") == "10.0.0.5:8080"
    assert _host("null") == "" and _host("") == ""
    assert verdict("GET", "/api/x", {"origin": "https://evil.example"}) is None
