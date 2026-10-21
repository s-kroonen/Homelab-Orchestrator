"""Deferred push: the database is the working copy, outside services are written
only on "Save to YAML" or an explicit "Push now".

Editing a proxy host (or, later, anything else that lives in an outside
service) changes the DB and nothing else. A :class:`Pusher` is the one thing
that knows how to bring one outside service in line with the DB:

* :meth:`Pusher.status` - read-only: what differs (``pending``), what changed
  behind our back (``detached``), what is waiting to be deleted.
* :meth:`Pusher.apply` - write it, per item, never stopping at the first
  failure, returning one :class:`PushResult` per thing done (or, with
  ``dry_run``, per thing that WOULD be done).

``ProxyPusher`` (:mod:`orchestrator.registry.proxy_push`) is the only one today.
A backup or power adapter that wants the same "edit locally, push on save"
behaviour implements the protocol and calls :func:`register_pusher`; the Save
button, ``POST /api/push``, ``GET /api/push/status`` and the CLI then cover it
without further changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlmodel import Session

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class PushResult:
    """One thing a push did (or would do)."""

    target: str
    hostname: str
    action: str
    ok: bool
    message: str = ""
    #: True for a dry-run plan entry: nothing was written.
    planned: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "hostname": self.hostname,
            "action": self.action,
            "ok": self.ok,
            "message": self.message,
            "planned": self.planned,
        }


@dataclass
class PushStatus:
    """What one pusher would have to do right now."""

    name: str
    #: False when the outside service could not be read; the classification is
    #: then based on our own bookkeeping only.
    reachable: bool = True
    in_sync: int = 0
    #: ``{"hostname", "state", "changes", "id"}`` - local edits not pushed yet.
    pending: list[dict[str, Any]] = field(default_factory=list)
    #: Same shape - our record is unchanged but the service differs.
    detached: list[dict[str, Any]] = field(default_factory=list)
    #: ``{"hostname", "route_ref", ...}`` - deleted here, still on the service.
    pending_deletes: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""

    @property
    def total(self) -> int:
        return len(self.pending) + len(self.detached) + len(self.pending_deletes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "reachable": self.reachable,
            "in_sync": self.in_sync,
            "pending": self.pending,
            "detached": self.detached,
            "pending_deletes": self.pending_deletes,
            "total": self.total,
            "message": self.message,
        }


class Pusher(Protocol):
    """An outside service the DB is eventually pushed to."""

    name: str

    async def status(self, session: Session, adapters: AdapterBundle) -> PushStatus: ...

    async def apply(
        self,
        session: Session,
        adapters: AdapterBundle,
        only: Sequence[str] | None = None,
        *,
        dry_run: bool = False,
        actor: str = "system",
    ) -> list[PushResult]: ...


PUSHERS: list[Pusher] = []


def register_pusher(pusher: Pusher) -> None:
    """Add ``pusher`` (replacing any already registered under its name)."""
    PUSHERS[:] = [p for p in PUSHERS if p.name != pusher.name]
    PUSHERS.append(pusher)


def get_pushers(names: Sequence[str] | None = None) -> list[Pusher]:
    # Importing registers the built-in pushers.
    from orchestrator.registry import proxy_push  # noqa: F401

    if not names:
        return list(PUSHERS)
    wanted = set(names)
    return [p for p in PUSHERS if p.name in wanted]


async def status_all(
    session: Session, adapters: AdapterBundle, names: Sequence[str] | None = None
) -> list[PushStatus]:
    out: list[PushStatus] = []
    for pusher in get_pushers(names):
        try:
            out.append(await pusher.status(session, adapters))
        except Exception as exc:  # a broken target must not hide the others
            log.warning("push.status_failed", target=pusher.name, error=str(exc))
            out.append(PushStatus(name=pusher.name, reachable=False, message=str(exc)))
    return out


async def apply_all(
    session: Session,
    adapters: AdapterBundle,
    *,
    names: Sequence[str] | None = None,
    only: Sequence[str] | None = None,
    dry_run: bool = False,
    actor: str = "system",
) -> list[PushResult]:
    """Run every (selected) pusher. A pusher that blows up becomes one failed
    item instead of an exception, so Save to YAML can always report."""
    results: list[PushResult] = []
    for pusher in get_pushers(names):
        try:
            results.extend(
                await pusher.apply(session, adapters, only, dry_run=dry_run, actor=actor)
            )
        except Exception as exc:
            log.warning("push.apply_failed", target=pusher.name, error=str(exc))
            session.rollback()
            results.append(
                PushResult(
                    target=pusher.name,
                    hostname="*",
                    action="push",
                    ok=False,
                    message=f"push to {pusher.name} failed: {exc}",
                )
            )
    return results


def summarize(results: Sequence[PushResult]) -> dict[str, Any]:
    done = [r for r in results if not r.planned]
    return {
        "attempted": len(done),
        "ok": sum(1 for r in done if r.ok),
        "failed": sum(1 for r in done if not r.ok),
    }
