"""The orchestrator's own network: can it reach its LAN router, the internet, the proxy?

Every health cycle starts here. The answer decides what a check that could not connect
means:

* the LAN router answers  -> the orchestrator's own network is fine, so a service that
  does not answer really is unreachable: DOWN (or FAILED when its guest is running);
* the LAN router does not -> nothing can be concluded about anything else: UNKNOWN,
  grouped under ``network:lan``;
* the internet does not   -> probes ticked "needs internet" are skipped, not failed.

A check is a TCP connect, so it needs no raw sockets: a host that answers at all, even
by refusing the port, is reachable. ``host`` alone tries 443, 80 and 53 in turn;
``host:port`` tries that port only.

The last result is kept in memory for the one-off scans between cycles (a backup's gate,
a wake) and in the ``network.status`` setting for the System page.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session

from orchestrator.config import Settings
from orchestrator.db.models import Setting
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

SETTING_KEY = "network.status"
_DEFAULT_PORTS = (443, 80, 53)
_TIMEOUT_S = 3.0

LABELS = {
    "lan": "LAN router",
    "internet": "Internet",
    "proxy": "Proxy (gateway)",
}


@dataclass
class NetCheck:
    name: str  # lan | internet | proxy
    target: str  # host or host:port as configured; "" = not configured
    #: True reachable, False not, None not configured.
    ok: bool | None = None
    latency_ms: float | None = None
    message: str = ""

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name)


@dataclass
class NetworkStatus:
    checks: list[NetCheck] = field(default_factory=list)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def _ok(self, name: str) -> bool | None:
        return next((c.ok for c in self.checks if c.name == name), None)

    @property
    def lan_ok(self) -> bool | None:
        return self._ok("lan")

    @property
    def internet_ok(self) -> bool | None:
        return self._ok("internet")

    @property
    def proxy_ok(self) -> bool | None:
        return self._ok("proxy")

    @property
    def own_network_ok(self) -> bool | None:
        """Can the orchestrator reach its own network? The LAN router decides; without
        one configured, any other check that answered proves it. None: nothing to go by."""
        if self.lan_ok is not None:
            return self.lan_ok
        answers = [c.ok for c in self.checks if c.ok is not None]
        return any(answers) if answers else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "own_network_ok": self.own_network_ok,
            "checks": [{**asdict(c), "label": c.label} for c in self.checks],
        }


def _split(target: str, default_ports: tuple[int, ...]) -> tuple[str, tuple[int, ...]]:
    value = target.strip()
    if "://" in value:
        parts = urlsplit(value)
        url_port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.hostname or "", (url_port,)
    host, sep, port = value.rpartition(":")
    if sep and port.isdigit() and host and not host.endswith(":"):
        return host.strip("[]"), (int(port),)
    return value.strip("[]"), default_ports


async def reach(target: str, *, timeout: float = _TIMEOUT_S) -> tuple[bool, float | None, str]:
    """``(reachable, latency_ms, message)`` for ``host``, ``host:port`` or a URL."""
    host, ports = _split(target, _DEFAULT_PORTS)
    if not host:
        return False, None, "no host"
    last = ""
    for port in ports:
        started = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except ConnectionRefusedError:
            # The host answered, by refusing the port: it is there.
            ms = round((time.monotonic() - started) * 1000, 1)
            return True, ms, f"{host} answered (port {port} refused)"
        except TimeoutError:
            last = f"{host}:{port} did not answer within {timeout:g}s"
            continue
        except OSError as exc:
            if exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
                return False, None, f"no route to {host}"
            last = f"{host}:{port}: {exc.strerror or exc}"
            continue
        ms = round((time.monotonic() - started) * 1000, 1)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True, ms, f"{host}:{port} answered"
    return False, None, last or f"{host} did not answer"


def targets(settings: Settings) -> list[tuple[str, str]]:
    """``[(name, target)]`` in check order; a blank target is "not configured"."""
    return [
        ("lan", settings.network_check_lan.strip()),
        ("internet", settings.network_check_internet.strip()),
        ("proxy", settings.probe_proxy_base_url.strip()),
    ]


async def check_network(settings: Settings) -> NetworkStatus:
    """Run every configured check at once. Never raises."""

    async def one(name: str, target: str) -> NetCheck:
        if not target:
            return NetCheck(name, "", None, None, "not configured")
        try:
            ok, ms, msg = await reach(target)
        except Exception as exc:  # pragma: no cover - defensive
            ok, ms, msg = False, None, f"{type(exc).__name__}: {exc}"
        return NetCheck(name, target, ok, ms, msg)

    checks = await asyncio.gather(*(one(n, t) for n, t in targets(settings)))
    status = NetworkStatus(checks=list(checks))
    remember(status)
    return status


_latest: NetworkStatus | None = None


def remember(status: NetworkStatus | None) -> None:
    global _latest
    _latest = status


def latest(max_age_s: float | None = None) -> NetworkStatus | None:
    """The last cycle's result, if it is recent enough to judge by."""
    if _latest is None:
        return None
    if max_age_s is not None and datetime.now(UTC) - _latest.at > timedelta(seconds=max_age_s):
        return None
    return _latest


def save(session: Session, status: NetworkStatus) -> None:
    row = session.get(Setting, SETTING_KEY)
    if row is None:
        session.add(Setting(key=SETTING_KEY, value=status.as_dict()))
    else:
        row.value = status.as_dict()
        row.updated_at = datetime.now(UTC)
        session.add(row)


def load(session: Session) -> dict[str, Any] | None:
    row = session.get(Setting, SETTING_KEY)
    return dict(row.value) if row is not None and row.value else None


__all__ = [
    "LABELS",
    "NetCheck",
    "NetworkStatus",
    "check_network",
    "latest",
    "load",
    "reach",
    "remember",
    "save",
    "targets",
]
