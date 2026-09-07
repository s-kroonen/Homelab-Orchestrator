"""Adapter exception hierarchy.

The distinction that matters most is **unreachable vs refused**:

* :class:`AdapterUnreachable` — we could not talk to the thing at all (DNS,
  TCP, TLS, timeout).  This is *indeterminate* and must map to
  ``HealthState.UNKNOWN`` at the gate — never to FAILED, because a network
  blip must not look like corruption.
* :class:`AdapterRequestError` — we talked to it and it said no.  This is a
  definitive negative.

Everything else is a subtype of one of those two ideas.
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base for every adapter-raised error."""


class AdapterUnreachable(AdapterError):
    """Transport failed: DNS, connect, TLS, or timeout. Indeterminate."""


class AdapterAuthError(AdapterError):
    """401/403 — token missing, wrong, or lacking the required privilege."""


class AdapterRequestError(AdapterError):
    """Non-2xx response that isn't an auth failure."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class TaskFailed(AdapterError):
    """A Proxmox/PBS background task finished with a non-OK exit status."""

    def __init__(self, message: str, *, upid: str, exit_status: str | None = None) -> None:
        super().__init__(message)
        self.upid = upid
        self.exit_status = exit_status


class TaskTimeout(AdapterError):
    """A background task did not finish within the allotted time.

    Indeterminate, like :class:`AdapterUnreachable` — the task may still be
    running server-side, so callers must not assume failure.
    """

    def __init__(self, message: str, *, upid: str) -> None:
        super().__init__(message)
        self.upid = upid
