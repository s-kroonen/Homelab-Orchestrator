"""Soft delete: a deleted service waits in the archive, can be restored, and is purged
after its retention. Also the not-found page that tells you so."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import ArchivedService, Node, Probe, ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.registry import archive as arch
from orchestrator.web.app import create_app
from tests.test_registry_api import HTTP_CONFIG, _make_node, _make_service, _register

HTML = {"accept": "text/html"}


@pytest.fixture
def app() -> Any:
    session_module.create_all_for_tests()
    return create_app()


@pytest.fixture
def client(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(app) as c:
        _register(c, monkeypatch, "storm")
        yield c


def _with_probe_and_host(client: TestClient, slug: str, node_id: int) -> None:
    _make_service(client, node_id, slug)
    r = client.post(
        f"/api/registry/services/{slug}/probes",
        json={"name": "web", "kind": "http", "config": HTTP_CONFIG},
    )
    assert r.status_code == 201, r.text
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == slug)).one()
        s.add(
            ProxyHost(service_id=svc.id, hostname=f"{slug}.example.com", upstream="http://1.2.3.4")
        )
        s.commit()


def test_delete_archives_and_restore_brings_it_all_back(client: TestClient) -> None:
    node = _make_node(client)
    _with_probe_and_host(client, "media", node["id"])
    _make_service(client, node["id"], "media-db", parent="media", guest_kind="none")
    assert client.delete("/api/registry/services/media").status_code == 409  # has a sub-service
    r = client.delete("/api/registry/services/media?cascade=true")
    assert r.status_code == 200 and "archived_until" in r.json()
    assert client.get("/api/registry/services/media").status_code == 404

    [entry] = client.get("/api/archive").json()
    assert entry["slug"] == "media" and entry["sub_services"] == ["media-db"]
    assert entry["probes"] == 1 and entry["proxy_hosts"] == ["media.example.com"]
    assert entry["days_left"] in (29, 30)

    r = client.post(f"/api/archive/{entry['id']}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["restored"] == ["media", "media-db"]
    assert client.get("/api/archive").json() == []
    with Session(get_engine()) as s:
        media = s.exec(select(Service).where(Service.slug == "media")).one()
        sub = s.exec(select(Service).where(Service.slug == "media-db")).one()
        assert sub.parent_id == media.id and media.node_id == node["id"]
        assert [
            p.name for p in s.exec(select(Probe).where(Probe.service_id == media.id)).all()
        ] == ["web"]
        host = s.exec(select(ProxyHost).where(ProxyHost.service_id == media.id)).one()
        assert host.hostname == "media.example.com" and host.pushed_sig == ""


def test_restore_refuses_what_cannot_work_and_changes_nothing(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], "media")
    client.delete("/api/registry/services/media")
    [entry] = client.get("/api/archive").json()

    _make_service(client, node["id"], "media")  # someone made a new one meanwhile
    r = client.post(f"/api/archive/{entry['id']}/restore")
    assert r.status_code == 409 and "exist" in r.json()["detail"]

    client.delete("/api/registry/services/media")
    with Session(get_engine()) as s:
        s.delete(s.exec(select(Node)).one())
        s.commit()
    newest = client.get("/api/archive").json()[0]
    r = client.post(f"/api/archive/{newest['id']}/restore")
    assert r.status_code == 409 and "node" in r.json()["detail"]
    assert client.get("/api/archive").json() and not _live("media")


def _live(slug: str) -> bool:
    with Session(get_engine()) as s:
        return s.exec(select(Service).where(Service.slug == slug)).first() is not None


def test_entries_are_purged_after_their_retention(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], "old")
    _make_service(client, node["id"], "fresh")
    client.delete("/api/registry/services/old")
    client.delete("/api/registry/services/fresh")
    with Session(get_engine()) as s:
        old = s.exec(select(ArchivedService).where(ArchivedService.slug == "old")).one()
        old.purge_at = datetime.now(UTC) - timedelta(minutes=1)
        s.add(old)
        s.commit()
    assert [e["slug"] for e in client.get("/api/archive").json()] == ["fresh"]
    with Session(get_engine()) as s:
        fresh = s.exec(select(ArchivedService)).one()
        assert (fresh.purge_at - fresh.deleted_at).days == get_settings().archive_retention_days

    [entry] = client.get("/api/archive").json()
    assert client.delete(f"/api/archive/{entry['id']}").status_code == 200
    assert client.get("/api/archive").json() == []
    assert client.delete(f"/api/archive/{entry['id']}").status_code == 404


def test_a_reload_that_drops_a_service_archives_it(client: TestClient) -> None:
    node = _make_node(client)
    _with_probe_and_host(client, "media", node["id"])
    with Session(get_engine()) as s:
        made = arch.archive_removed(s, get_settings(), ["media"], "registry-reload")
        s.commit()
        assert [a.slug for a in made] == ["media"] and made[0].deleted_by == "registry-reload"
        assert arch.drop_revived(s) == 1  # it is still live, so the entry is stale


def test_not_found_page_knows_a_deleted_service(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], "media")
    client.delete("/api/registry/services/media")

    r = client.get("/services/media", headers=HTML)
    assert r.status_code == 404 and "text/html" in r.headers["content-type"]
    assert "was deleted" in r.text and "Restore media" in r.text

    r = client.get("/services/never-existed", headers=HTML)
    assert r.status_code == 404 and "No service called" in r.text and "Restore" not in r.text

    r = client.get("/no/such/page", headers=HTML)
    assert r.status_code == 404 and "Page not found" in r.text

    # machines keep the JSON error
    assert client.get("/api/registry/services/media").json() == {"detail": "no service 'media'"}
    assert client.get("/services/media").status_code == 404  # no html accept: still a 404


def test_not_found_page_does_not_tell_strangers_what_existed(client: TestClient, app: Any) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], "media")
    client.delete("/api/registry/services/media")
    with TestClient(app) as stranger:
        r = stranger.get("/no/such/page", headers=HTML)
        assert r.status_code == 404 and "media" not in r.text
        r = stranger.get("/services/media", headers=HTML, follow_redirects=False)
        assert r.status_code == 303  # the login wall comes first


def test_archive_page_needs_login(app: Any) -> None:
    with TestClient(app) as anon:
        r = anon.get("/archive", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert anon.get("/api/archive").status_code == 401


# -- proxy hosts and backup policies ------------------------------------------------------------


def _host(slug: str, hostname: str) -> int:
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == slug)).one()
        ph = ProxyHost(service_id=svc.id, hostname=hostname, upstream="http://1.2.3.4:80")
        s.add(ph)
        s.commit()
        s.refresh(ph)
        assert ph.id is not None
        return ph.id


def test_a_deleted_proxy_host_can_be_restored(client: TestClient) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], "media")
    host_id = _host("media", "tv.example.com")
    assert client.delete(f"/api/proxy-hosts/{host_id}").status_code == 200

    [entry] = client.get("/api/archive").json()
    assert entry["kind"] == "proxy_host" and entry["slug"] == "tv.example.com"
    assert "http://1.2.3.4:80" in entry["summary"] and "media" in entry["summary"]

    r = client.get(f"/proxy-hosts/{host_id}/edit", headers=HTML)
    assert r.status_code == 404 and "was deleted" in r.text and "proxy host" in r.text

    r = client.post(f"/api/archive/{entry['id']}/restore")
    assert r.status_code == 200, r.text
    with Session(get_engine()) as s:
        ph = s.exec(select(ProxyHost).where(ProxyHost.hostname == "tv.example.com")).one()
        assert ph.upstream == "http://1.2.3.4:80" and ph.pushed_sig == ""
    assert client.get("/api/archive").json() == []


def test_a_proxy_host_is_not_restored_without_its_service_or_onto_a_taken_name(
    client: TestClient,
) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], "media")
    host_id = _host("media", "tv.example.com")
    client.delete(f"/api/proxy-hosts/{host_id}")
    [entry] = client.get("/api/archive").json()

    _host("media", "tv.example.com")  # the name is in use again
    r = client.post(f"/api/archive/{entry['id']}/restore")
    assert r.status_code == 409 and "used by a proxy host" in r.json()["detail"]

    with Session(get_engine()) as s:
        for ph in s.exec(select(ProxyHost)).all():
            s.delete(ph)
        s.commit()
    client.delete("/api/registry/services/media")
    newest = next(e for e in client.get("/api/archive").json() if e["kind"] == "proxy_host")
    r = client.post(f"/api/archive/{newest['id']}/restore")
    assert r.status_code == 409 and "service" in r.json()["detail"]


def test_a_deleted_backup_policy_comes_back_with_its_services(
    client: TestClient,
) -> None:
    node = _make_node(client)
    _make_service(client, node["id"], "media")
    _make_service(client, node["id"], "cloud")
    body = {
        "name": "nightly",
        "schedule_cron": "0 3 * * *",
        "mode": "snapshot",
        "retention": {"keep_daily": 7},
        "services": ["media", "cloud"],
    }
    assert client.post("/api/backup-policies", json=body).status_code == 201
    assert client.delete("/api/backup-policies/nightly").status_code == 200

    [entry] = client.get("/api/archive").json()
    assert entry["kind"] == "backup_policy" and "media, cloud" in entry["summary"]
    r = client.get("/backup-policies/nightly/edit", headers=HTML)
    assert r.status_code == 404 and "was deleted" in r.text

    # one of them joined another policy meanwhile
    other = {**body, "name": "weekly", "services": ["cloud"]}
    assert client.post("/api/backup-policies", json=other).status_code == 201

    r = client.post(f"/api/archive/{entry['id']}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["restored"][0] == "nightly" and "cloud" in " ".join(r.json()["warnings"])
    policies = {p["name"]: p for p in client.get("/api/backup-policies").json()}
    assert policies["nightly"]["services"] == ["media"]
    assert policies["nightly"]["schedule_cron"] == "0 3 * * *"
    assert policies["nightly"]["scheduled"] is True  # the scheduler was told
