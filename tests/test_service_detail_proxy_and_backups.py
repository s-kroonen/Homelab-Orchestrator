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
    # Adding and editing open the proxy host drawer, pre-filled for this service.
    js = client.get("/static/service.js").text
    assert "O.proxyHostDrawer({" in js and "serviceId: S.id" in js
    assert '"/api/proxy-hosts"' in js  # each host's push state


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
    assert "Backups" in page.text and 'id="backups-card"' in page.text
    assert '"/api/backups?slug="' in client.get("/static/service.js").text


def test_service_page_has_manual_backup_trigger_button(client: TestClient) -> None:
    """Template smoke test only — POST /api/backup/{slug} itself is already
    tested in test_backup_pipeline.py / test_backup_api.py."""
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media")

    page = client.get(f"/services/{svc['slug']}")
    assert page.status_code == 200, page.text
    js = client.get("/static/service.js").text
    assert '"Back up now"' in js and "function backupNow" in js


def test_service_page_shows_guest_power_buttons_when_service_has_a_guest(
    client: TestClient,
) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media", guest_id=100)

    page = client.get(f"/services/{svc['slug']}")
    assert page.status_code == 200, page.text
    # The guest's start / restart / stop sit in the header's menu, through the controller API.
    js = client.get("/static/service.js").text
    assert '"Start the guest"' in js and '"Stop the guest…"' in js
    assert 'base + "/actions/" + action' in js and "/controller?live=true" in js


def test_service_page_hides_guest_power_row_when_service_has_no_guest(
    client: TestClient,
) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media", guest_kind="none")

    page = client.get(f"/services/{svc['slug']}")
    assert page.status_code == 200, page.text
    # Offered only when the controller says the guest can be acted on.
    assert "const guestCtl = ctlInfo && ctlInfo.guest && ctlInfo.guest.available" in (
        client.get("/static/service.js").text
    )


def test_service_page_renders_tablist_and_four_panels_with_counts(client: TestClient) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media")
    for name in ("web", "port"):
        r = client.post(
            f"/api/registry/services/{svc['slug']}/probes",
            json={"name": name, "kind": "tcp", "config": {"host": "10.0.0.9", "port": 80}},
        )
        assert r.status_code == 201, r.text
    r = client.post(
        "/api/proxy-hosts",
        json={
            "hostname": "media.example.com",
            "upstream": "http://10.0.0.9:80",
            "service_id": svc["id"],
        },
    )
    assert r.status_code == 201, r.text

    html = client.get(f"/services/{svc['slug']}").text
    assert 'role="tablist"' in html
    for tab in ("overview", "configuration", "related", "history"):
        assert f'<button role="tab" data-tab="{tab}">' in html
        assert f'<div role="tabpanel" data-tab="{tab}" hidden>' in html
    # plus Sub-services, shown when there are any (or for a root, to add one)
    assert html.count('role="tab" ') == 5 and 'data-tab="subs" id="tab-subs" hidden' in html
    # The counts come from the data in the page.
    view = client.get(f"/api/views/service/{svc['slug']}").json()
    assert len(view["service"]["probes"]) == 2 and len(view["service"]["proxy_hosts"]) == 1
    js = client.get("/static/service.js").text
    assert '$("#n-probes").textContent' in js and '$("#n-related").textContent' in js
    # Old links to a part of the page land on the tab that holds it now.
    assert 'probes: "configuration"' in js and 'deploys: "related"' in js


def test_service_view_is_the_page_data(client: TestClient) -> None:
    node = _make_node(client)
    svc = _make_service(client, node["id"], slug="media")
    view = client.get(f"/api/views/service/{svc['slug']}").json()
    assert view["service"]["probes"] == [] and view["service"]["proxy_hosts"] == []
    assert view["row"]["state"]["label"] == "unknown" and view["health"]["verdict"] == "unknown"
    assert client.get("/api/views/service/nope").status_code == 404
