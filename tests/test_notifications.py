"""The notification engine, its channels, the API, and the scanner hook."""

from __future__ import annotations

import base64
import smtplib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.factory import build_adapters, build_notifier
from orchestrator.adapters.notifier.base import Notifier, NotifySeverity
from orchestrator.adapters.notifier.channels import NtfyNotifier, SmtpNotifier, WebhookNotifier
from orchestrator.adapters.notifier.multi import MultiNotifier
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest
from orchestrator.config import Settings, get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import NotificationEvent, NotificationRule, TargetState
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.notify.engine import NotificationEngine, Observation
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.scheduler.health_scanner import HealthScanner
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin


class Recorder(Notifier):
    """Stands in for the channels; remembers what each event sent to which."""

    def __init__(self, names: Sequence[str] = ("ntfy", "email"), fail: Sequence[str] = ()) -> None:
        self.names = list(names)
        self.fail = set(fail)
        self.sent: list[tuple[list[str] | None, str, str]] = []

    def channels(self) -> list[str]:
        return self.names

    async def notify(self, **kw: Any) -> None:  # pragma: no cover - notify_via is used
        raise AssertionError

    async def notify_via(
        self, channels: Sequence[str] | None, *, severity: NotifySeverity, title: str, **kw: Any
    ) -> dict[str, str]:
        wanted = list(channels) if channels else self.names
        self.sent.append((list(channels) if channels else None, severity.value, title))
        return {c: ("error: boom" if c in self.fail else "ok") for c in wanted}


@pytest.fixture
def session() -> Session:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        yield s


def engine_with(rec: Notifier, **overrides: Any) -> NotificationEngine:
    return NotificationEngine(rec, get_settings().model_copy(update=overrides))


def obs(
    key: str, state: str, *, level: str = "service", cause: str | None = None, **scope: str
) -> Observation:
    return Observation(level, key, key, state, f"{key} is {state}", cause_key=cause, scope=scope)


async def feed(
    engine: NotificationEngine, session: Session, *rounds: list[Observation]
) -> list[list[NotificationEvent]]:
    return [await engine.observe(session, r) for r in rounds]


async def test_first_sight_is_silent_and_a_change_needs_two_scans(session: Session) -> None:
    rec = Recorder()
    engine = engine_with(rec)
    out = await feed(
        engine,
        session,
        [obs("web", "healthy")],
        [obs("web", "failed")],  # first time we see it differ: not yet
        [obs("web", "failed")],  # confirmed
        [obs("web", "failed")],  # unchanged: nothing more
    )
    assert [len(x) for x in out] == [0, 0, 1, 0]
    assert out[2][0].event == "failed" and out[2][0].severity == "error"
    assert out[2][0].delivered == {"ntfy": "ok", "email": "ok"}
    assert [t for _, _, t in rec.sent] == ["web is FAILED"]


async def test_a_blip_never_announces(session: Session) -> None:
    engine = engine_with(Recorder())
    out = await feed(
        engine, session, [obs("web", "healthy")], [obs("web", "failed")], [obs("web", "healthy")]
    )
    assert [len(x) for x in out] == [0, 0, 0]


async def test_recovery_is_announced_only_for_what_was_announced(session: Session) -> None:
    engine = engine_with(Recorder())
    out = await feed(
        engine,
        session,
        [obs("web", "healthy")],
        [obs("web", "failed")],
        [obs("web", "failed")],
        [obs("web", "healthy")],
        [obs("web", "healthy")],
    )
    assert [e.event for batch in out for e in batch] == ["failed", "recovered"]


async def test_unknown_is_ignored(session: Session) -> None:
    engine = engine_with(Recorder())
    out = await feed(engine, session, [obs("web", "healthy")], *([[obs("web", "unknown")]] * 4))
    assert sum(len(x) for x in out) == 0
    assert session.get(TargetState, ("service", "web")).state == "healthy"  # type: ignore[union-attr]


async def test_effects_fold_into_the_root_cause_and_do_not_page(session: Session) -> None:
    rec = Recorder()
    engine = engine_with(rec)
    healthy = [
        obs("pve1", "healthy", level="node"),
        obs("web", "healthy", cause="node:pve1"),
    ]
    down = [
        obs("pve1", "down", level="node"),
        obs("web", "down", cause="node:pve1"),
    ]
    out = await feed(engine, session, healthy, down, down, healthy, healthy)
    events = [e for batch in out for e in batch]
    announced = [(e.target, e.event) for e in events if not e.suppressed]
    folded = [(e.target, e.suppressed_reason) for e in events if e.suppressed]
    assert announced == [("pve1", "down"), ("pve1", "recovered")]
    assert folded == [("web", "covered by node:pve1")]
    assert len(rec.sent) == 2  # one page down, one page up - not four


async def test_flapping_alerts_once_then_mutes(session: Session) -> None:
    rec = Recorder()
    engine = engine_with(rec, notify_confirm_scans=1, notify_flap_threshold=4)
    states = ["healthy", "failed", "healthy", "failed", "healthy", "failed", "healthy"]
    out = await feed(engine, session, *[[obs("web", s)] for s in states])
    names = [e.event for batch in out for e in batch if not e.suppressed]
    assert names.count("flapping") == 1
    assert len([e for batch in out for e in batch if e.suppressed]) >= 1
    titles = [t for _, _, t in rec.sent]
    assert any("flapping" in t for t in titles)


async def test_rules_pick_levels_scope_severity_and_channels(session: Session) -> None:
    rec = Recorder(names=["ntfy", "email", "webhook"])
    engine = engine_with(rec, notify_confirm_scans=1)
    session.add(
        NotificationRule(
            name="phone",
            levels=["service"],
            events=["failed"],
            channels=["ntfy"],
            min_severity="error",
        )
    )
    session.add(
        NotificationRule(
            name="media only", scope={"groups": ["media"]}, events=["down"], channels=["email"]
        )
    )
    session.commit()
    await feed(
        engine,
        session,
        [
            obs("a", "healthy", group="home"),
            obs("b", "healthy", group="media"),
            obs("n", "healthy", level="node"),
        ],
        [
            obs("a", "failed", group="home"),
            obs("b", "down", group="media"),
            obs("n", "down", level="node"),
        ],
    )
    sent = sorted((tuple(c or ()), t) for c, _, t in rec.sent)
    assert sent == [(("email",), "b is DOWN"), (("ntfy",), "a is FAILED")]
    rows = {r.target: r for r in session.exec(select(NotificationEvent)).all()}
    assert rows["n"].suppressed and rows["n"].suppressed_reason == "no rule wants this"
    assert rows["a"].rule_ids and rows["a"].delivered == {"ntfy": "ok"}


async def test_default_announces_the_important_ones_only(session: Session) -> None:
    rec = Recorder()
    engine = engine_with(rec, notify_confirm_scans=1)
    await feed(engine, session, [obs("a", "healthy")], [obs("a", "failed")])
    from orchestrator.notify.engine import Event

    skipped = await engine.emit(
        session, Event("service", "a", "a", "recovery_attempt", "info", "t", "b")
    )
    assert skipped.suppressed and skipped.suppressed_reason == "not announced by default"
    sent = await engine.emit(
        session, Event("service", "a", "a", "recovery_exhausted", "error", "x", "y")
    )
    assert not sent.suppressed and sent.delivered == {"ntfy": "ok", "email": "ok"}


async def test_a_dead_channel_is_reported_not_raised(session: Session) -> None:
    engine = engine_with(Recorder(fail=["email"]), notify_confirm_scans=1)
    out = await feed(engine, session, [obs("a", "healthy")], [obs("a", "failed")])
    assert out[1][0].delivered == {"ntfy": "ok", "email": "error: boom"}


async def test_disabled_engine_records_nothing(session: Session) -> None:
    engine = engine_with(Recorder(), notifications_enabled=False, notify_confirm_scans=1)
    out = await feed(engine, session, [obs("a", "healthy")], [obs("a", "failed")])
    assert out == [[], []]
    assert session.exec(select(TargetState)).all() == []


# --- channels ----------------------------------------------------------------


async def test_ntfy_sends_headers_and_encodes_non_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    await NtfyNotifier("https://ntfy.test/topic", "tok").notify(
        severity=NotifySeverity.CRITICAL,
        title="Café down",
        body="detail",
        tags=["service"],
        approve_url="https://x/y",
    )
    (req,) = seen
    assert req.headers["priority"] == "5" and req.headers["authorization"] == "Bearer tok"
    assert req.headers["title"].startswith("=?UTF-8?B?")
    assert base64.b64decode(req.headers["title"][10:-2]).decode() == "Café down"
    assert "rotating_light" in req.headers["tags"] and req.headers["click"] == "https://x/y"
    assert req.content == b"detail"


async def test_webhook_posts_json_and_failures_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(500)), **kw),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await WebhookNotifier("https://hook.test").notify(
            severity=NotifySeverity.INFO, title="t", body="b"
        )


async def test_smtp_builds_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            sent.append(("connect", host, port))

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def starttls(self) -> None:
            sent.append("starttls")

        def login(self, u: str, p: str) -> None:
            sent.append(("login", u))

        def send_message(self, m: Any) -> None:
            sent.append((m["Subject"], m["To"], m.get_content().strip()))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    await SmtpNotifier(
        host="mail.test",
        port=587,
        username="u",
        password="p",
        sender="o@x",
        recipients=["a@x", "b@x"],
    ).notify(severity=NotifySeverity.ERROR, title="web is FAILED", body="why")
    assert sent[0] == ("connect", "mail.test", 587) and "starttls" in sent
    assert sent[-1] == ("[orchestrator error] web is FAILED", "a@x, b@x", "why")


async def test_multi_notifier_isolates_channels() -> None:
    class Boom(Notifier):
        async def notify(self, **kw: Any) -> None:
            raise RuntimeError("down")

    class Fine(Notifier):
        async def notify(self, **kw: Any) -> None:
            return None

    multi = MultiNotifier({"a": Boom(), "b": Fine()})
    out = await multi.notify_via(None, severity=NotifySeverity.INFO, title="t", body="b")
    assert out["b"] == "ok" and out["a"].startswith("error: down")
    only = await multi.notify_via(["b", "nope"], severity=NotifySeverity.INFO, title="t", body="b")
    assert only["b"] == "ok" and "not configured" in only["nope"]
    with pytest.raises(RuntimeError):
        await MultiNotifier({"a": Boom()}).notify(severity=NotifySeverity.INFO, title="t", body="b")


def test_build_notifier_from_settings() -> None:
    base = Settings(dry_run=False, notifier_adapter="auto")
    assert isinstance(build_notifier(base), NullNotifier)  # nothing configured
    s = base.model_copy(update={"ntfy_url": "https://n/t", "notify_webhook_url": "https://w"})
    assert build_notifier(s).channels() == ["ntfy", "webhook"]
    mail = s.model_copy(update={"mail_smtp_host": "m", "mail_to": "a@x, b@x"})
    assert build_notifier(mail).channels() == ["ntfy", "email", "webhook"]
    only = mail.model_copy(update={"notifier_adapter": "email"})
    assert build_notifier(only).channels() == ["email"]
    assert isinstance(build_notifier(mail.model_copy(update={"dry_run": True})), NullNotifier)
    assert isinstance(
        build_notifier(mail.model_copy(update={"notifier_adapter": "null"})), NullNotifier
    )


# --- API ---------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_rules_crud_and_validation(client: TestClient) -> None:
    assert client.get("/api/notifications/rules").json() == []
    ok = client.post(
        "/api/notifications/rules",
        json={
            "name": "phone",
            "levels": ["service"],
            "events": ["failed"],
            "channels": ["ntfy"],
            "min_severity": "error",
            "scope": {"slugs": [" a ", ""]},
        },
    )
    assert ok.status_code == 201, ok.text
    rule = ok.json()
    assert rule["scope"] == {"slugs": ["a"]}
    assert client.post("/api/notifications/rules", json={"name": "phone"}).status_code == 409
    for bad in (
        {"name": "x", "levels": ["galaxy"]},
        {"name": "x", "events": ["boom"]},
        {"name": "x", "channels": ["pigeon"]},
        {"name": "x", "min_severity": "loud"},
        {"name": " "},
        {"name": "x", "scope": {"colour": ["red"]}},
    ):
        assert client.post("/api/notifications/rules", json=bad).status_code == 400, bad
    upd = client.put(
        f"/api/notifications/rules/{rule['id']}", json={"name": "phone2", "enabled": False}
    )
    assert upd.status_code == 200 and upd.json()["enabled"] is False
    assert client.delete(f"/api/notifications/rules/{rule['id']}").json() == {"deleted": "phone2"}
    assert client.delete(f"/api/notifications/rules/{rule['id']}").status_code == 404


def test_test_endpoint_reports_each_channel_and_feed_lists_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(fail=["email"])
    client.app.state.adapters.notifier = rec  # type: ignore[attr-defined]
    client.app.state.notifications._notifier = rec  # type: ignore[attr-defined]
    chans = client.get("/api/notifications/channels").json()
    assert chans["channels"] == ["ntfy", "email"]
    r = client.post("/api/notifications/test", json={}).json()
    assert r["delivered"] == {"ntfy": "ok", "email": "error: boom"}
    only = client.post("/api/notifications/test", json={"channels": ["ntfy"]}).json()
    assert only["delivered"] == {"ntfy": "ok"}
    feed = client.get("/api/notifications/events").json()
    assert [e["event"] for e in feed] == ["test", "test"]
    assert client.get("/notifications").status_code == 200
    assert client.get("/api/notifications/events?level=node").json() == []


def test_silent_notifier_says_why(client: TestClient) -> None:
    chans = client.get("/api/notifications/channels").json()
    assert chans["channels"] == [] and "No channel is active" in chans["hint"]
    r = client.post("/api/notifications/test", json={}).json()
    assert r["suppressed"] and r["suppressed_reason"] == "no channel is active"


# --- the scanner hook ----------------------------------------------------------


REGISTRY = """
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: portainer
    name: Portainer
    node: pve1
    guest_kind: vm
    guest_id: 101
  - slug: nginx
    name: nginx
    parent: portainer
"""


async def test_scanner_announces_a_dead_node_once_and_folds_the_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    settings = get_settings()
    adapters = build_adapters(settings)
    proxmox = adapters.proxmox
    proxmox.simulated_guests = [  # type: ignore[attr-defined]
        Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="docker", status="running")
    ]
    proxmox.simulated_cluster = ClusterStatus(nodes=[ClusterNode("pve1", online=True)])  # type: ignore[attr-defined]
    rec = Recorder()
    scanner = HealthScanner(
        settings=settings, adapters=adapters, notifications=NotificationEngine(rec, settings)
    )
    await scanner.poll_once()  # baseline: everything healthy/unknown, silence
    assert rec.sent == []

    proxmox.simulated_cluster = ClusterStatus(nodes=[ClusterNode("pve1", online=False)])  # type: ignore[attr-defined]
    await scanner.poll_once()
    await scanner.poll_once()
    await scanner.poll_once()

    titles = [t for _, _, t in rec.sent]
    assert titles == ["node pve1 is DOWN"], titles
    with Session(get_engine()) as s:
        rows = s.exec(select(NotificationEvent)).all()
    folded = {r.target for r in rows if r.suppressed and r.suppressed_reason.startswith("covered")}
    assert {"portainer", "nginx"} <= folded


async def test_hooks_announce_is_fail_open_and_uses_the_installed_engine(session: Session) -> None:
    from orchestrator.notify import hooks

    await hooks.announce(
        session,
        level="service",
        key="a",
        name="A",
        event="backup_failed",
        severity="error",
        title="t",
        body="b",
    )
    assert session.exec(select(NotificationEvent)).all() == []  # no engine installed: silent
    rec = Recorder()
    hooks.install(engine_with(rec))
    try:
        await hooks.announce(
            session,
            level="service",
            key="a",
            name="A",
            event="backup_failed",
            severity="error",
            title="Backup of A FAILED",
            body="b",
        )
    finally:
        hooks.install(None)
    (row,) = session.exec(select(NotificationEvent)).all()
    assert row.event == "backup_failed" and not row.suppressed, row.suppressed_reason
    assert [t for _, _, t in rec.sent] == ["Backup of A FAILED"]
