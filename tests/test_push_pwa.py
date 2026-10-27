"""The PWA (manifest, service worker) and Web Push (subscriptions, VAPID, delivery)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pywebpush import WebPushException
from sqlmodel import Session, select

from orchestrator.adapters.notifier.base import NotifySeverity
from orchestrator.adapters.notifier.multi import MultiNotifier
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import PushSubscription
from orchestrator.db.session import get_engine, session_scope
from orchestrator.notify import push as push_mod
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

SUB = {
    "endpoint": "https://push.example.test/send/abc123",
    "keys": {"p256dh": "BPubKeyPubKey", "auth": "authsecret"},
    "label": "Pixel",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(push_mod, "webpush", fake)
    return calls


def test_manifest_and_service_worker_are_public_and_installable(client: TestClient) -> None:
    with TestClient(create_app()) as anon:
        m = anon.get("/manifest.webmanifest")
        assert m.status_code == 200 and m.headers["content-type"].startswith(
            "application/manifest+json"
        )
        body = m.json()
        assert body["display"] == "standalone" and body["start_url"] == "/"
        sizes = {i["sizes"] for i in body["icons"]}
        assert {"192x192", "512x512"} <= sizes
        for icon in body["icons"]:
            assert anon.get(icon["src"]).status_code == 200

        sw = anon.get("/sw.js")
        assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
        assert sw.headers["service-worker-allowed"] == "/"
        assert "no-cache" in sw.headers["cache-control"]
        assert 'addEventListener("push"' in sw.text
        assert "notificationclick" in sw.text


def test_every_page_links_the_manifest_and_loads_pwa_js(client: TestClient) -> None:
    html = client.get("/notifications").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert "/static/pwa.js" in html and 'id="dev-enable"' in html
    assert client.get("/static/pwa.js").status_code == 200


def test_pwa_js_runs_before_the_pages_own_scripts(client: TestClient) -> None:
    """The notifications page calls OrchPush from an inline script; a `defer`red pwa.js would
    only run after it ("OrchPush is not defined")."""
    html = client.get("/notifications").text
    start = html.index('<script src="/static/pwa.js?v=')
    tag = html[start : html.index("</script>", start) + len("</script>")]
    assert (
        tag in html
        and "defer"
        not in html[html.index("/static/pwa.js") - 20 : html.index("/static/pwa.js") + 40]
    )
    assert html.index(tag) < html.index("await OrchPush.status()")


def test_vapid_key_is_created_once_and_is_a_p256_point(client: TestClient) -> None:
    first = client.get("/api/push/key").json()["public_key"]
    again = client.get("/api/push/key").json()["public_key"]
    assert first == again
    raw = base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))
    assert len(raw) == 65 and raw[0] == 4  # uncompressed point


def test_push_api_needs_a_login() -> None:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as anon:
        assert anon.get("/api/push/key").status_code == 401
        assert anon.post("/api/push/subscribe", json=SUB).status_code == 401


def test_subscribe_list_resubscribe_and_remove(client: TestClient) -> None:
    r = client.post("/api/push/subscribe", json=SUB)
    assert r.status_code == 200 and r.json()["label"] == "Pixel"
    client.post("/api/push/subscribe", json={**SUB, "label": "Pixel 8"})
    rows = client.get("/api/push/subscriptions").json()
    assert len(rows) == 1 and rows[0]["label"] == "Pixel 8"  # same endpoint = same device

    assert (
        client.post("/api/push/subscribe", json={**SUB, "endpoint": "http://x"}).status_code == 400
    )
    assert client.delete(f"/api/push/subscriptions/{rows[0]['id']}").json() == {"removed": True}
    assert client.get("/api/push/subscriptions").json() == []
    assert client.delete("/api/push/subscriptions/999").status_code == 404
    actions = [e["action"] for e in client.get("/api/activity?kind=admin").json()]
    assert {"push.subscribed", "push.resubscribed", "push.removed"} <= set(actions)


def test_test_push_encrypts_for_the_device_and_records_the_result(
    client: TestClient, sent: list[dict[str, Any]]
) -> None:
    assert client.post("/api/push/test", json={}).status_code == 409  # nobody yet
    client.post("/api/push/subscribe", json=SUB)

    r = client.post("/api/push/test", json={"endpoint": SUB["endpoint"]})

    assert r.status_code == 200 and r.json() == {"results": {"Pixel": "ok"}, "ok": True}
    (call,) = sent
    assert call["subscription_info"] == {"endpoint": SUB["endpoint"], "keys": SUB["keys"]}
    assert call["vapid_claims"]["sub"].startswith(("mailto:", "https://"))
    payload = json.loads(call["data"])
    assert payload["title"] == "Test notification" and payload["url"] == "/notifications"
    row = client.get("/api/push/subscriptions").json()[0]
    assert row["last_ok_at"] and row["last_error"] == ""


def test_a_device_the_push_service_has_forgotten_is_removed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/push/subscribe", json=SUB)

    def gone(**_kw: Any) -> None:
        raise WebPushException("gone", response=SimpleNamespace(status_code=410))

    monkeypatch.setattr(push_mod, "webpush", gone)
    r = client.post("/api/push/test", json={})
    assert r.json()["ok"] is False and "unsubscribed" in r.json()["results"]["Pixel"]
    assert client.get("/api/push/subscriptions").json() == []


def test_a_failing_push_service_keeps_the_device_and_says_why(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/push/subscribe", json=SUB)

    def down(**_kw: Any) -> None:
        raise WebPushException("boom", response=SimpleNamespace(status_code=503))

    monkeypatch.setattr(push_mod, "webpush", down)
    r = client.post("/api/push/test", json={})
    assert "503" in r.json()["results"]["Pixel"]
    row = client.get("/api/push/subscriptions").json()[0]
    assert "503" in row["last_error"]


async def test_the_push_channel_reports_skipped_without_devices_and_ok_with_one(
    client: TestClient, sent: list[dict[str, Any]]
) -> None:
    channel = MultiNotifier({"push": push_mod.WebPushNotifier(get_settings())})

    nobody = await channel.notify_via(
        None, severity=NotifySeverity.ERROR, title="Plex is FAILED", body="x"
    )
    assert nobody["push"].startswith("skipped:")  # not an error: nobody to tell
    await channel.notify(severity=NotifySeverity.ERROR, title="t", body="b")  # does not raise

    client.post("/api/push/subscribe", json=SUB)
    done = await channel.notify_via(
        None,
        severity=NotifySeverity.ERROR,
        title="Plex is FAILED",
        body="down",
        approve_url="/approve/x",
    )
    assert done == {"push": "ok"}
    payload = json.loads(sent[-1]["data"])
    assert payload["title"] == "Plex is FAILED" and payload["url"] == "/approve/x"
    assert payload["severity"] == "error"


def test_the_build_offers_a_push_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import config as config_module
    from orchestrator.adapters.factory import build_notifier

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("NOTIFIER_ADAPTER", "auto")
    config_module.reset_settings_cache()
    assert "push" in build_notifier(get_settings()).channels()
    monkeypatch.setenv("NOTIFIER_ADAPTER", "push")
    config_module.reset_settings_cache()
    assert build_notifier(get_settings()).channels() == ["push"]


def test_subscription_table_exists_in_the_models() -> None:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        assert s.exec(select(PushSubscription)).all() == []
    with session_scope() as s:
        assert s.exec(select(PushSubscription)).all() == []
