"""Live updates: the event bus, the database hooks that feed it, and the SSE stream."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator import events
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, PipelineRun, Probe, Service, ServiceState
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import HealthState, PipelineKind, PipelineStatus, ProbeKind
from orchestrator.web.app import create_app


@pytest.fixture(autouse=True)
def hooks() -> None:
    session_module.create_all_for_tests()
    events.install_db_hooks()


async def _collect(sub: events.Subscription, want: int, timeout: float = 1.0) -> list[events.Event]:
    out: list[events.Event] = []
    while len(out) < want:
        ev = await sub.next(timeout)
        if ev is None:
            break
        out.append(ev)
    return out


async def test_a_committed_write_publishes_what_changed() -> None:
    sub = events.bus.subscribe()
    try:
        with Session(get_engine()) as s:
            svc = Service(slug="media", name="media")
            s.add(svc)
            s.commit()
            s.add(
                PipelineRun(
                    kind=PipelineKind.DEPLOY,
                    correlation_id="c",
                    service_id=svc.id,
                    status=PipelineStatus.RUNNING,
                )
            )
            s.add(ServiceState(service_id=svc.id, last_verdict=HealthState.HEALTHY))
            s.add(AuditEntry(actor="t", action="x.y", target="service:media"))
            s.commit()
            got = await _collect(sub, 4)
        by_topic = {e.topic: e.data for e in got}
        assert {"registry", "run", "health", "audit"} <= set(by_topic)
        assert by_topic["run"]["kind"] == "deploy" and by_topic["run"]["status"] == "running"
        assert by_topic["health"]["verdict"] == "healthy"
        assert by_topic["audit"] == {"action": "x.y", "target": "service:media"}
    finally:
        events.bus.unsubscribe(sub)


async def test_an_update_to_a_run_is_published_too() -> None:
    with Session(get_engine()) as s:
        run = PipelineRun(kind=PipelineKind.WAKE, correlation_id="c", status=PipelineStatus.RUNNING)
        s.add(run)
        s.commit()
        sub = events.bus.subscribe()
        try:
            run.status = PipelineStatus.SUCCEEDED
            s.add(run)
            s.commit()
            (ev,) = await _collect(sub, 1)
            assert ev.topic == "run" and ev.data["status"] == "succeeded"
        finally:
            events.bus.unsubscribe(sub)


async def test_a_rolled_back_write_publishes_nothing() -> None:
    sub = events.bus.subscribe()
    try:
        with Session(get_engine()) as s:
            s.add(Service(slug="ghost", name="ghost"))
            s.flush()
            s.rollback()
        assert await _collect(sub, 1, timeout=0.2) == []
    finally:
        events.bus.unsubscribe(sub)


async def test_the_same_row_written_twice_in_one_commit_is_one_event() -> None:
    sub = events.bus.subscribe()
    try:
        with Session(get_engine()) as s:
            svc = Service(slug="m", name="m")
            s.add(svc)
            s.flush()
            svc.name = "m2"
            s.add(svc)
            s.commit()
        got = await _collect(sub, 5, timeout=0.2)
        assert [e.topic for e in got] == ["registry"]
    finally:
        events.bus.unsubscribe(sub)


async def test_probe_changes_name_their_service() -> None:
    sub = events.bus.subscribe()
    try:
        with Session(get_engine()) as s:
            svc = Service(slug="p", name="p")
            s.add(svc)
            s.commit()
            await _collect(sub, 1, timeout=0.2)
            s.add(Probe(service_id=svc.id, name="http", kind=ProbeKind.HTTP, config={}))
            s.commit()
            (ev,) = await _collect(sub, 1)
            assert ev.data == {"what": "probe", "service_id": svc.id}
    finally:
        events.bus.unsubscribe(sub)


async def test_writes_from_other_threads_arrive_and_nobody_listening_costs_nothing() -> None:
    events.bus.publish("run", run_id=1)  # no subscriber: a no-op, not an error
    sub = events.bus.subscribe()
    try:
        t = threading.Thread(target=lambda: events.bus.publish("health", service_id=7))
        t.start()
        t.join()
        (ev,) = await _collect(sub, 1)
        assert ev == events.Event("health", {"service_id": 7})
    finally:
        events.bus.unsubscribe(sub)


async def test_a_slow_reader_loses_its_oldest_events_only() -> None:
    sub = events.bus.subscribe()
    try:
        for i in range(events.QUEUE_MAX + 5):
            sub.push(events.Event("run", {"i": i}))
        assert sub.dropped == 5
        first = await sub.next(0.1)
        assert first is not None and first.data == {"i": 5}
    finally:
        events.bus.unsubscribe(sub)


async def test_the_stream_says_hello_sends_events_and_pings_when_idle() -> None:
    sub = events.bus.subscribe()
    gone = False

    async def disconnected() -> bool:
        return gone

    frames = events.stream(sub, disconnected, heartbeat_s=0.05)
    try:
        assert await anext(frames) == "retry: 3000\n\n"
        assert (await anext(frames)).startswith("event: hello\n")
        events.bus.publish("run", run_id=3, status="running")
        frame = await anext(frames)
        assert frame.startswith("event: run\n")
        assert json.loads(frame.split("data: ", 1)[1].strip()) == {"run_id": 3, "status": "running"}
        assert await anext(frames) == ": ping\n\n"
        gone = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(frames), 1)
    finally:
        events.bus.unsubscribe(sub)


def test_the_endpoint_needs_a_login(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(create_app()) as c:
        assert c.get("/api/events").status_code == 401


def test_the_endpoint_streams_events_to_a_logged_in_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a real socket: an in-process TestClient cannot read a response that never ends."""
    import socket
    import time

    import httpx
    import uvicorn

    from tests import test_proxy_hosts as th

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base = f"http://127.0.0.1:{port}"
    th._fake_verify_registration(monkeypatch, b"cred-1")
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            try:
                httpx.get(base + "/healthz", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        admin = httpx.Client(base_url=base)
        assert (
            admin.post(
                "/api/auth/register/begin", json={"username": "storm", "invite_token": None}
            ).status_code
            == 200
        )
        reg = admin.post(
            "/api/auth/register/complete",
            json={
                "username": "storm",
                "device_label": "t",
                "invite_token": None,
                "credential": th._credential(b"cred-1"),
            },
        )
        assert reg.status_code == 200, reg.text
        with admin.stream("GET", "/api/events", timeout=10) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["x-accel-buffering"] == "no"
            lines = r.iter_lines()
            assert next(lines) == "retry: 3000"
            while next(lines) != "event: hello":
                pass
            # a write made (by anything in this process) while the page is connected
            with Session(get_engine()) as s:
                s.add(AuditEntry(actor="t", action="live.test", target=""))
                s.commit()
            seen: list[str] = []
            for line in lines:
                seen.append(line)
                if line.startswith("data:") and "live.test" in line:
                    break
            assert "event: audit" in seen
    finally:
        server.should_exit = True
        thread.join(5)
