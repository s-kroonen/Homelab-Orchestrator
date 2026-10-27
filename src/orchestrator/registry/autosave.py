"""One save model: a change is applied when you press Save, and that is all you do.

Every registry change (service, probe, proxy host, node) commits to the database and publishes a
``registry`` event (``orchestrator.events``). The :class:`Autosaver` listens for those and, after
a short quiet period, does what the "Save to YAML" and "Push" buttons used to:

1. writes ``services.yaml`` (keeping the previous file in ``STATE_DIR/registry-history``), and
2. pushes the pending proxy-host changes to Traefik Manager.

What could not be done (the YAML could not be written, a push failed) is kept in
:attr:`Autosaver.state` and shown in one place, the pending pill in the header (``GET
/api/pending``), with a Retry. Nothing is retried on its own: a push that fails keeps failing until
someone looks.

``REGISTRY_AUTOSAVE=false`` turns it off; Save to YAML and Push then work as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.config import Settings
from orchestrator.db.session import session_scope
from orchestrator.events import bus
from orchestrator.logging_setup import get_logger
from orchestrator.registry.loader import compute_registry_diff, save_registry_to_yaml
from orchestrator.registry.push import apply_all, status_all

log = get_logger(__name__)

#: Seconds of quiet after the last change before saving: a bulk edit saves once.
QUIET_S = 1.5
#: Previous versions of services.yaml kept in STATE_DIR/registry-history.
HISTORY_KEEP = 30


def history_dir(settings: Settings) -> Path:
    return Path(settings.state_dir) / "registry-history"


def keep_previous(settings: Settings) -> Path | None:
    """Copy the current services.yaml into the history before it is overwritten."""
    src = Path(settings.services_yaml_path)
    if not src.exists():
        return None
    folder = history_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    dest = folder / f"services-{stamp}.yaml"
    shutil.copy2(src, dest)
    for old in sorted(folder.glob("services-*.yaml"))[:-HISTORY_KEEP]:
        old.unlink(missing_ok=True)
    return dest


@dataclass
class AutosaveState:
    #: When services.yaml was last written by the autosaver.
    saved_at: str | None = None
    #: Why the last write failed (None: it worked).
    save_error: str | None = None
    #: When the last push ran, and the hosts it could not push.
    pushed_at: str | None = None
    push_failed: list[dict[str, Any]] = field(default_factory=list)
    #: Changes not pushed yet, per the last status check (None: not checked yet).
    push_pending: int | None = None
    push_reachable: bool = True
    push_message: str = ""
    running: bool = False
    #: time.monotonic() of the last change seen; until it is saved, that change is "saving",
    #: not "pending".
    changed_at: float = 0.0

    def settling(self, within_s: float = QUIET_S + 8) -> bool:
        return self.running or (time.monotonic() - self.changed_at) < within_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "saved_at": self.saved_at,
            "save_error": self.save_error,
            "pushed_at": self.pushed_at,
            "push_failed": self.push_failed,
            "push_pending": self.push_pending,
            "push_reachable": self.push_reachable,
            "push_message": self.push_message,
            "running": self.running,
        }


class Autosaver:
    def __init__(self, *, settings: Settings, adapters: AdapterBundle) -> None:
        self._settings = settings
        self._adapters = adapters
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.state = AutosaveState()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.registry_autosave)

    def start(self) -> None:
        if not self.enabled:
            log.info("autosave.disabled", reason="REGISTRY_AUTOSAVE=false")
            return
        self._task = asyncio.create_task(self._loop(), name="registry-autosave")
        log.info("autosave.started", quiet_s=QUIET_S)

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        sub = bus.subscribe()
        try:
            # What is pending since before the restart (a push that failed then).
            await self.refresh_status()
            while True:
                ev = await sub.next(3600)
                if ev is None or ev.topic != "registry":
                    continue
                self.state.changed_at = time.monotonic()
                # Wait for the burst to end: every change in it is saved together.
                while True:
                    nxt = await sub.next(QUIET_S)
                    if nxt is None:
                        break
                await self.run_once(actor="autosave")
                # Our own writes (sync bookkeeping after a push) come back as events: drop them.
                while await sub.next(0.05) is not None:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - never let the loop die silently
            log.error("autosave.loop_failed", error=str(exc), error_type=type(exc).__name__)
        finally:
            bus.unsubscribe(sub)

    async def run_once(self, *, actor: str = "autosave", push: bool = True) -> AutosaveState:
        """Write services.yaml if the database differs from it, then push. Never raises."""
        async with self._lock:
            self.state.running = True
            bus.publish("pending", running=True)
            try:
                await self._save(actor)
                if push:
                    await self._push(actor)
                    # A push records what it did (the route it created) on the proxy host:
                    # write that too, so services.yaml is not one step behind.
                    await self._save(actor, keep=False)
            finally:
                self.state.running = False
                bus.publish("pending", running=False)
        return self.state

    async def _save(self, actor: str, *, keep: bool = True) -> None:
        path = Path(self._settings.services_yaml_path)
        try:
            with session_scope() as session:
                if not compute_registry_diff(session).dirty:
                    self.state.save_error = None
                    return
                if keep:
                    keep_previous(self._settings)
                save_registry_to_yaml(session, path, actor=actor)
            self.state.saved_at = datetime.now(UTC).isoformat()
            self.state.save_error = None
            log.info("autosave.saved", path=str(path))
        except Exception as exc:
            self.state.save_error = f"could not write {path}: {exc}"
            log.warning("autosave.save_failed", path=str(path), error=str(exc))

    async def _push(self, actor: str) -> None:
        try:
            with session_scope() as session:
                statuses = await status_all(session, self._adapters)
                todo = sum(st.total for st in statuses)
                self._remember_status(statuses)
                if not todo:
                    self.state.push_failed = []
                    return
                results = await apply_all(session, self._adapters, actor=actor)
            done = [r for r in results if not r.planned]
            self.state.pushed_at = datetime.now(UTC).isoformat()
            self.state.push_failed = [r.as_dict() for r in done if not r.ok]
            log.info(
                "autosave.pushed",
                ok=sum(1 for r in done if r.ok),
                failed=len(self.state.push_failed),
            )
        except Exception as exc:
            self.state.push_failed = [
                {
                    "target": "proxy",
                    "hostname": "*",
                    "action": "push",
                    "ok": False,
                    "message": str(exc),
                }
            ]
            log.warning("autosave.push_failed", error=str(exc))
        await self.refresh_status()

    async def refresh_status(self) -> None:
        """Re-read what is not pushed yet (asks Traefik Manager). Never raises."""
        try:
            with session_scope() as session:
                self._remember_status(await status_all(session, self._adapters))
        except Exception as exc:
            self.state.push_reachable = False
            self.state.push_message = str(exc)
        bus.publish("pending", running=self.state.running)

    def _remember_status(self, statuses: list[Any]) -> None:
        self.state.push_pending = sum(len(st.pending) + len(st.pending_deletes) for st in statuses)
        self.state.push_reachable = all(st.reachable for st in statuses)
        self.state.push_message = "; ".join(st.message for st in statuses if st.message)
