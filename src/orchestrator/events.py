"""Live updates: a small in-process event bus, fed by database changes, read by Server-Sent Events.

Pages used to reload themselves every 30-60 seconds. Now anything that changes the data they
show publishes a small "something changed" event, ``GET /api/events`` streams those to the
browser, and a page refreshes the part that matters when one arrives (static/live.js). Events carry
no state - just *what kind of thing* changed and which service, so a missed one costs nothing: the
page asks the API for the truth.

The source is the database itself (:func:`install_db_hooks`): after a commit that wrote a pipeline
run, a health verdict, an audit row, a notification, or a registry row, the matching event is
published - so every pipeline, controller and API call is covered with no per-call plumbing.

Topics and their data:

* ``run``      a pipeline run (wake / backup / restore / switch / deploy) changed: ``run_id``,
               ``kind``, ``status``, ``service_id``
* ``health``   a service's verdict was written: ``service_id``, ``verdict``
* ``audit``    something was done: ``action``, ``target``
* ``alert``    the notification engine recorded an event
* ``registry`` a service, probe, proxy host or node changed: ``what``, ``service_id`` when known

Everything runs in one process, so this is an in-memory fan-out; a subscriber that cannot keep up
loses its oldest events (they are only hints).
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session as SASession

from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Per subscriber; beyond this the oldest are dropped.
QUEUE_MAX = 200
#: How often an idle stream sends a comment, so proxies and browsers do not time it out.
HEARTBEAT_S = 15.0


@dataclass(frozen=True)
class Event:
    topic: str
    data: dict[str, Any] = field(default_factory=dict)


class Subscription:
    """One connected client: an asyncio queue that drops its oldest entry when full."""

    def __init__(self) -> None:
        self._items: deque[Event] = deque(maxlen=QUEUE_MAX)
        self._wake = asyncio.Event()
        self.dropped = 0

    def push(self, ev: Event) -> None:
        if len(self._items) == self._items.maxlen:
            self.dropped += 1
        self._items.append(ev)
        self._wake.set()

    async def next(self, timeout: float) -> Event | None:
        """The next event, or None after ``timeout`` seconds of quiet."""
        if not self._items:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                return None
        return self._items.popleft() if self._items else None


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self) -> Subscription:
        """Call from the event loop that will read it."""
        sub = Subscription()
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)

    def publish(self, topic: str, **data: Any) -> None:
        """Safe from any thread, never blocks, never raises, free when nobody listens."""
        if not self._subs:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        ev = Event(topic, data)
        try:
            loop.call_soon_threadsafe(self._deliver, ev)
        except RuntimeError:  # the loop is shutting down
            return

    def _deliver(self, ev: Event) -> None:
        for sub in list(self._subs):
            sub.push(ev)


bus = EventBus()


def format_frame(ev: Event) -> str:
    import json

    return f"event: {ev.topic}\ndata: {json.dumps(ev.data, separators=(',', ':'))}\n\n"


async def stream(
    sub: Subscription,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    heartbeat_s: float = HEARTBEAT_S,
) -> AsyncIterator[str]:
    """The SSE body: a hello, then events as they come and a comment when idle, until the client
    goes away."""
    yield "retry: 3000\n\n"
    yield format_frame(Event("hello", {}))
    while True:
        ev = await sub.next(heartbeat_s)
        if ev is not None:
            yield format_frame(ev)
            continue
        if await is_disconnected():
            return
        yield ": ping\n\n"


# -- the database as the source ----------------------------------------------------------------


def _describe(obj: Any) -> tuple[str, str, dict[str, Any]] | None:
    """(topic, dedup key, data) for a row whose change is worth telling pages about."""
    from orchestrator.db import models as m

    if isinstance(obj, m.PipelineRun):
        return (
            "run",
            f"run:{obj.id}",
            {
                "run_id": obj.id,
                "kind": getattr(obj.kind, "value", str(obj.kind)),
                "status": getattr(obj.status, "value", str(obj.status)),
                "service_id": obj.service_id,
            },
        )
    if isinstance(obj, m.ServiceState):
        return (
            "health",
            f"health:{obj.service_id}",
            {
                "service_id": obj.service_id,
                "verdict": getattr(obj.last_verdict, "value", str(obj.last_verdict)),
            },
        )
    if isinstance(obj, m.AuditEntry):
        return "audit", f"audit:{obj.id}", {"action": obj.action, "target": obj.target}
    if isinstance(obj, m.NotificationEvent):
        return "alert", f"alert:{obj.id}", {}
    if isinstance(obj, m.Service):
        return "registry", f"service:{obj.id}", {"what": "service", "service_id": obj.id}
    if isinstance(obj, m.Probe | m.ProxyHost):
        return (
            "registry",
            f"{type(obj).__name__}:{obj.id}",
            {"what": type(obj).__name__.lower(), "service_id": getattr(obj, "service_id", None)},
        )
    if isinstance(obj, m.Node):
        return "registry", f"node:{obj.id}", {"what": "node"}
    return None


_KEY = "live_events"


def _after_flush(session: SASession, _ctx: Any) -> None:
    if not bus.subscribers:
        return
    pending: dict[str, tuple[str, dict[str, Any]]] = session.info.setdefault(_KEY, {})
    for obj in (*session.new, *session.dirty):
        try:
            found = _describe(obj)
        except Exception:  # pragma: no cover - never let a hint break a write
            continue
        if found is not None:
            topic, key, data = found
            pending[key] = (topic, data)


def _after_commit(session: SASession) -> None:
    pending = session.info.pop(_KEY, None)
    if not pending:
        return
    for topic, data in pending.values():
        bus.publish(topic, **data)


def _discard(session: SASession, *_args: Any) -> None:
    session.info.pop(_KEY, None)


_installed = False


def install_db_hooks() -> None:
    """Publish an event after every committed write that matters. Idempotent."""
    global _installed
    if _installed:
        return
    event.listen(SASession, "after_flush", _after_flush)
    event.listen(SASession, "after_commit", _after_commit)
    event.listen(SASession, "after_rollback", _discard)
    _installed = True
