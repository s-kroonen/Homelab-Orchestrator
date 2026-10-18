"""Parser for Traefik's own access-log lines.

Traefik's access log supports exactly two formats (its own docs, "Logs and
access logs"): a `common` format — an extension of the classic Apache/NCSA
Combined Log Format with four extra, Traefik-specific fields appended — and
a `json` format, one JSON object per line, using the same field set under Go
field names (`ClientHost`, `RouterName`, `RequestPath`, ...). `common` is
Traefik's own default when `accessLog` is enabled with no `format` given.

This module supports BOTH, since it is genuinely not much harder to parse
one line-based JSON object than one regex line — the two parsers below are
each a few lines. What format a given deployment actually uses is entirely
the operator's own Traefik static config (`accessLog.format`), not
something this app controls or can detect in advance beyond sniffing each
line, so :func:`parse_line` tries JSON first (a line starting with `{`) and
falls back to the common-format regex.

**JSON is the recommended format.** Only it carries ``RequestHost`` (the
request's own ``Host`` header), which attributes a hit to a proxy host
directly — the common format only names the router, which then has to be
resolved to hostnames through Traefik Manager (see
``orchestrator.scheduler.access_log_collector``).

**Never raises.** A line that matches neither shape — a truncated line, a
format this module doesn't recognise, a blank line, Traefik Manager's own
non-log noise — is reported back as ``None`` so the caller (``orchestrator
.scheduler.access_log_collector.AccessLogCollector``) can count and skip it,
the same fail-open philosophy as every other optional integration in this
app. A single garbled line must never crash the collector or corrupt the
rest of that poll's counts.

The common-format field order mirrors Traefik's own documented default
(`ClientHost - ClientUsername [StartLocal] "Method Path Protocol"
OriginStatus OriginContentSize "Referer" "User-Agent" RequestCount
"RouterName" "ServiceURL" Duration`), e.g.::

    10.0.0.1 - - [10/Jun/2026:08:42:00 +0000] "GET / HTTP/1.1" 200 1337 \
"-" "-" 1 "my-router@docker" "http://10.0.0.2:80" 1ms

The JSON format carries the same information under Go field names, e.g.
``{"ClientHost":"10.0.0.1","StartUTC":"2026-06-10T08:42:00Z",
"RouterName":"my-router@docker","RequestHost":"app.example.com",
"RequestPath":"/","DownstreamStatus":200,"Duration":1234567,...}``
(``Duration`` in nanoseconds).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class AccessLogEntry:
    """One parsed access-log line, backend/format-neutral.

    ``router`` is Traefik's router name (e.g. ``my-router@docker``) — the
    name Traefik Manager's ``/api/traefik/routers`` reports, which is one
    way :class:`~orchestrator.scheduler.access_log_collector
    .AccessLogCollector` attributes a hit to a ``ProxyHost``/``Service``.

    ``host`` is the request's own ``Host`` (JSON format's ``RequestHost``,
    port stripped, lower-cased) — the most direct attribution there is, but
    only the JSON format carries it; ``None`` for common-format lines.
    ``duration_ms`` is the request's total duration in milliseconds
    (``None`` when the line doesn't carry one).
    """

    client_ip: str
    timestamp: datetime
    router: str
    status: int
    path: str
    raw: str
    host: str | None = None
    duration_ms: float | None = None


def normalize_router_name(name: str) -> str:
    """``my-router@docker`` -> ``my-router``. Traefik's access log and its
    runtime API name routers ``<name>@<provider>``; Traefik Manager's own
    ``/api/routes`` lists file-provider routes by the bare name. Comparing
    the stripped form on both sides is what lets the two meet."""
    return name.strip().split("@", 1)[0].lower()


def normalize_host(host: str | None) -> str | None:
    """Lower-cased hostname with any ``:port`` stripped (bracketed IPv6
    literals kept intact). ``None``/blank -> ``None``."""
    if not host:
        return None
    h = host.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        h = h[: end + 1] if end != -1 else h
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    h = h.rstrip(".")
    return h or None


_UNIT_TO_MS = {
    "ns": 1e-6,
    "us": 1e-3,
    "µs": 1e-3,
    "μs": 1e-3,
    "ms": 1.0,
    "s": 1000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
}
_GO_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)")


def parse_go_duration_ms(text: str) -> float | None:
    """A Go ``time.Duration`` string (Traefik's common-format last field:
    ``12ms``, ``1.5s``, ``350µs``, ``1m2.5s``) -> milliseconds. ``None`` for
    anything else (``-``, blank, garbage)."""
    s = text.strip()
    if not s or s == "-":
        return None
    total = 0.0
    pos = 0
    for m in _GO_DURATION_PART.finditer(s):
        if m.start() != pos:
            return None
        total += float(m.group(1)) * _UNIT_TO_MS[m.group(2)]
        pos = m.end()
    if pos == 0 or pos != len(s):
        return None
    return total


# -- common (extended CLF) ----------------------------------------------------
#
# ClientHost "-" ClientUsername [StartLocal] "Method Path Proto" Status Size
# "Referer" "UserAgent" RequestCount "RouterName" "ServiceURL" Duration
_COMMON_RE = re.compile(
    r"^(?P<host>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "
    r'"(?P<method>\S+) (?P<path>\S+) \S+" '
    r"(?P<status>\d{3}) \S+ "
    r'"[^"]*" "[^"]*" \S+ '
    r'"(?P<router>[^"]*)" "[^"]*" (?P<duration>\S+)$'
)

# Apache/NCSA log timestamp, e.g. "10/Jun/2026:08:42:00 +0000".
_COMMON_TS_FMT = "%d/%b/%Y:%H:%M:%S %z"


def _parse_common(line: str) -> AccessLogEntry | None:
    m = _COMMON_RE.match(line.strip())
    if m is None:
        return None
    try:
        ts = datetime.strptime(m["ts"], _COMMON_TS_FMT)
    except ValueError:
        return None
    return AccessLogEntry(
        client_ip=m["host"],
        timestamp=ts.astimezone(UTC),
        router=m["router"],
        status=int(m["status"]),
        path=m["path"],
        raw=line,
        duration_ms=parse_go_duration_ms(m["duration"]),
    )


# -- json -----------------------------------------------------------------


def _parse_json(line: str) -> AccessLogEntry | None:
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    host = obj.get("ClientHost")
    if not isinstance(host, str) or not host:
        # ClientAddr is "ip:port" — fall back to it, same as Traefik's own
        # JSON logger (ClientHost is normally just the split-out form of it).
        addr = obj.get("ClientAddr")
        host = addr.rsplit(":", 1)[0] if isinstance(addr, str) and ":" in addr else None
    if not isinstance(host, str) or not host:
        return None

    ts_raw = obj.get("StartUTC") or obj.get("StartLocal") or obj.get("time")
    if not isinstance(ts_raw, str):
        return None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)

    router = obj.get("RouterName")
    if router is None:
        # A request no router matched (Traefik's own 404) carries no
        # RouterName at all; it can still be attributed by RequestHost.
        router = ""
    status = obj.get("DownstreamStatus")
    if status is None:
        status = obj.get("OriginStatus")
    path = obj.get("RequestPath")
    if not isinstance(router, str) or not isinstance(status, int) or not isinstance(path, str):
        return None

    # JSON "Duration" is Go's time.Duration marshalled as an integer number
    # of nanoseconds.
    duration_raw = obj.get("Duration")
    duration_ms = (
        float(duration_raw) / 1e6
        if isinstance(duration_raw, int | float) and not isinstance(duration_raw, bool)
        else None
    )
    request_host = obj.get("RequestHost")

    return AccessLogEntry(
        client_ip=host,
        timestamp=ts.astimezone(UTC),
        router=router,
        status=status,
        path=path,
        raw=line,
        host=normalize_host(request_host) if isinstance(request_host, str) else None,
        duration_ms=duration_ms,
    )


def parse_line(line: str) -> AccessLogEntry | None:
    """Parse one raw access-log line. ``None`` on anything that doesn't
    match either known shape — never raises. Caller counts and skips."""
    stripped = line.strip()
    if not stripped:
        return None
    if stripped[0] == "{":
        return _parse_json(stripped)
    return _parse_common(stripped)


def parse_lines(lines: list[str]) -> list[AccessLogEntry]:
    """Parse every line, silently dropping ones that don't match either
    format (each dropped line is logged at debug level; the collector
    reports the count in its status). A batch of garbage must never raise,
    only shrink the result."""
    entries: list[AccessLogEntry] = []
    for line in lines:
        entry = parse_line(line)
        if entry is None:
            if line.strip():
                log.debug("access_log.unparsable_line", line=line[:200])
            continue
        entries.append(entry)
    return entries


__all__ = [
    "AccessLogEntry",
    "normalize_host",
    "normalize_router_name",
    "parse_go_duration_ms",
    "parse_line",
    "parse_lines",
]
