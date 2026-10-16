"""Service-page smoke tests for two additive, UI-only small features (see
docs/roadmap.md): an inline "Add a proxy host" form + this service's own
proxy hosts list, and a "Backups" section. Both call EXISTING, already
tested endpoints (``POST/DELETE /api/proxy-hosts[/{id}]``, ``GET
/api/backups?slug=``) — this only proves the template renders what it
should, not those endpoints themselves.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_registry_api import _make_node, _make_service
from tests.test_registry_api import app as app
from tests.test_registry_api import client as client


def test_service_page_renders_proxy_hosts_section_and_form(client: TestClient) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media")

    r = client.post(
        "/api/proxy-hosts",
        json={
            "hostname": "media.example.com",
            "upstream": "http://10.0.0.9:8080",
            "service_id": svc["id"],
        },
    )
    assert r.status_code == 201, r.text

    page = client.get(f"/services/{svc['slug']}")
    assert page.status_code == 200, page.text
    assert "Proxy hosts" in page.text
    assert "media.example.com" in page.text
    # No more inline add form — a link to the dedicated add page, pre-filled
    # with this service's id, replaces it (see docs/proxy_hosts.md).
    assert f'href="/proxy-hosts/new?service_id={svc["id"]}"' in page.text
    assert "/api/proxy-hosts" in page.text


def test_service_page_proxy_host_list_is_scoped_to_this_service(client: TestClient) -> None:
    node = _make_node(client)
    media = _make_service(client, node["id"], slug="media")
    other = _make_service(client, node["id"], slug="other")

    client.post(
        "/api/proxy-hosts", json={"hostname": "media.example.com", "service_id": media["id"]}
    )
    client.post(
        "/api/proxy-hosts", json={"hostname": "other.example.com", "service_id": other["id"]}
    )

    detail = client.get(f"/api/registry/services/{media['slug']}").json()
    hostnames = {h["hostname"] for h in detail["proxy_hosts"]}
    assert hostnames == {"media.example.com"}


def test_service_page_renders_backups_section(client: TestClient) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media")

    page = client.get(f"/services/{svc['slug']}")
    assert page.status_code == 200, page.text
    assert "Backups" in page.text
    assert "backups-body" in page.text
    assert f"/api/backups?slug={svc['slug']}" in page.text


def test_service_page_has_manual_backup_trigger_button(client: TestClient) -> None:
    """Template smoke test only — POST /api/backup/{slug} itself is already
    tested in test_backup_pipeline.py / test_backup_api.py."""
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media")

    page = client.get(f"/services/{svc['slug']}")
    assert page.status_code == 200, page.text
    assert 'id="run-backup-btn"' in page.text
    assert "/api/backup/${encodeURIComponent(SERVICE.slug)}?verify=true" in page.text


def test_service_page_shows_guest_power_buttons_when_service_has_a_guest(
    client: TestClient,
) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media", guest_id=100)

    page = client.get(f"/services/{svc['slug']}")
    assert page.status_code == 200, page.text
    assert 'id="guest-start-btn"' in page.text
    assert 'id="guest-stop-btn"' in page.text
    assert "/api/services/${encodeURIComponent(SERVICE.slug)}/guest/power" in page.text


def test_service_page_hides_guest_power_row_when_service_has_no_guest(
    client: TestClient,
) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media", guest_kind="none")

    page = client.get(f"/services/{svc['slug']}")
    assert page.status_code == 200, page.text
    # The buttons are still in the markup (hidden client-side by JS, same
    # pattern as #wake-unsupported-banner) — this proves the hiding logic
    # runs, not that the markup is absent.
    assert 'guest-power-row").hidden = !hasGuest' in page.text
