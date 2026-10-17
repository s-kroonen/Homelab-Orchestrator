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

**Never raises.** A line that matches neither shape — a truncated line, a
format this module doesn't recognise, a blank line, Traefik Manager's own
non-log noise — is reported back as ``None`` so the caller (``orchestrator
.scheduler.geo_traffic_collector.GeoTrafficCollector``) can log and skip it,
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
"RouterName":"my-router@docker","RequestPath":"/","DownstreamStatus":200,...}``.
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
    same identifier :func:`orchestrator.registry.traefik_status
    .fetch_router_details` resolves to a set of hostnames, which is how
    :class:`~orchestrator.scheduler.geo_traffic_collector.GeoTrafficCollector`
    attributes a hit to a ``ProxyHost``/``Service``, mirroring
    ``TrafficCollector``'s own router -> hostname -> ProxyHost lookup.
    """

    client_ip: str
    timestamp: datetime
    router: str
    status: int
    path: str
    raw: str


# -- common (extended CLF) ----------------------------------------------------
#
# ClientHost "-" ClientUsername [StartLocal] "Method Path Proto" Status Size
# "Referer" "UserAgent" RequestCount "RouterName" "ServiceURL" Duration
_COMMON_RE = re.compile(
    r"^(?P<host>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "
    r'"(?P<method>\S+) (?P<path>\S+) \S+" '
    r"(?P<status>\d{3}) \S+ "
    r'"[^"]*" "[^"]*" \S+ '
    r'"(?P<router>[^"]*)" "[^"]*" \S+$'
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
    status = obj.get("DownstreamStatus")
    if status is None:
        status = obj.get("OriginStatus")
    path = obj.get("RequestPath")
    if not isinstance(router, str) or not isinstance(status, int) or not isinstance(path, str):
        return None

    return AccessLogEntry(
        client_ip=host,
        timestamp=ts.astimezone(UTC),
        router=router,
        status=status,
        path=path,
        raw=line,
    )


def parse_line(line: str) -> AccessLogEntry | None:
    """Parse one raw access-log line. ``None`` on anything that doesn't
    match either known shape — never raises. Caller logs and skips."""
    stripped = line.strip()
    if not stripped:
        return None
    if stripped[0] == "{":
        return _parse_json(stripped)
    return _parse_common(stripped)


def parse_lines(lines: list[str]) -> list[AccessLogEntry]:
    """Parse every line, silently dropping ones that don't match either
    format (each dropped line is logged at debug level; a batch of garbage
    must never raise, only shrink the result)."""
    entries: list[AccessLogEntry] = []
    for line in lines:
        entry = parse_line(line)
        if entry is None:
            if line.strip():
                log.debug("access_log.unparsable_line", line=line[:200])
            continue
        entries.append(entry)
    return entries


__all__ = ["AccessLogEntry", "parse_line", "parse_lines"]
