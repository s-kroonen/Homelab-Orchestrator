"""Pure helpers: turn Portainer / Docker JSON into the redacted shapes in ``base``.

Nothing here does I/O, so the parts that matter most - that no secret leaks, that the
log stream is decoded right - are tested directly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from orchestrator.adapters.portainer.base import (
    Container,
    ContainerDetail,
    ContainerStats,
    Environment,
    GitInfo,
    LogLine,
    Mount,
    PortMapping,
    Stack,
)

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
SWARM_STACK = "com.docker.stack.namespace"

_ENV_TYPES = {
    1: "docker",
    2: "agent",
    3: "azure",
    4: "edge",
    5: "kubernetes",
    6: "kubernetes",
    7: "edge",
}
_STACK_TYPES = {1: "swarm", 2: "compose", 3: "kubernetes"}
_HEALTH_RE = re.compile(r"\((?:health: )?(healthy|unhealthy|starting)\)")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})$")


# -- small conversions ----------------------------------------------------------------


def iso_from_unix(value: Any) -> str | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def iso_clean(value: Any) -> str | None:
    """Docker uses ``0001-01-01T00:00:00Z`` for "never"."""
    text = str(value or "")
    if not text or text.startswith("0001-01-01"):
        return None
    return text


def epoch_from_iso(ts: str | None) -> int | None:
    """Unix seconds from Docker's RFC 3339 timestamps, which carry nanoseconds
    (``2026-10-24T12:00:00.123456789Z``) that ``fromisoformat`` cannot read."""
    if not ts:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$", ts)
    if not m:
        return None
    base, frac, zone = m.groups()
    text = f"{base}.{(frac or '0')[:6].ljust(6, '0')}{'+00:00' if zone == 'Z' else zone}"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def redact_url(url: str) -> tuple[str, bool]:
    """A Git URL without ``user:token@``, and whether it had credentials."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", False
    if parts.username is None and parts.password is None:
        return url, False
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment)), True


# -- environments & stacks --------------------------------------------------------------


def environment(raw: dict[str, Any]) -> Environment:
    snaps = raw.get("Snapshots") or []
    snap = snaps[0] if snaps and isinstance(snaps[0], dict) else {}
    status = {1: "up", 2: "down"}.get(raw.get("Status"), "unknown")
    return Environment(
        id=int(raw["Id"]),
        name=str(raw.get("Name", "")),
        type=_ENV_TYPES.get(raw.get("Type"), "unknown"),
        status=status,
        url=str(raw.get("URL", "")),
        running=snap.get("RunningContainerCount"),
        stopped=snap.get("StoppedContainerCount"),
        healthy=snap.get("HealthyContainerCount"),
        unhealthy=snap.get("UnhealthyContainerCount"),
        stacks=snap.get("StackCount"),
    )


def stack(raw: dict[str, Any]) -> Stack:
    git_raw = raw.get("GitConfig") or None
    git: GitInfo | None = None
    if isinstance(git_raw, dict) and git_raw.get("URL"):
        url, had_creds = redact_url(str(git_raw["URL"]))
        had_creds = had_creds or bool(git_raw.get("Authentication"))
        git = GitInfo(
            url=url,
            reference=str(git_raw.get("ReferenceName", "")),
            path=str(git_raw.get("ConfigFilePath", "")),
            hash=str(git_raw.get("ConfigHash", "")),
            has_credentials=had_creds,
        )
    auto = raw.get("AutoUpdate") or {}
    env_names = tuple(
        str(e.get("name", ""))
        for e in (raw.get("Env") or [])
        if isinstance(e, dict) and e.get("name")
    )
    return Stack(
        id=int(raw["Id"]),
        name=str(raw.get("Name", "")),
        endpoint_id=int(raw.get("EndpointId", 0)),
        type=_STACK_TYPES.get(raw.get("Type"), "unknown"),
        status="active" if raw.get("Status") == 1 else "inactive",
        managed=True,
        git=git,
        auto_update=bool(auto.get("Interval") or auto.get("Webhook")),
        auto_update_interval=str(auto.get("Interval") or ""),
        has_webhook=bool(auto.get("Webhook")),
        env_names=env_names,
        created_at=iso_from_unix(raw.get("CreationDate")),
        updated_at=iso_from_unix(raw.get("UpdateDate")),
        created_by=str(raw.get("CreatedBy", "") or ""),
        updated_by=str(raw.get("UpdatedBy", "") or ""),
    )


def external_stack(name: str, endpoint_id: int, kind: str, running: int) -> Stack:
    """A compose project nobody created through Portainer, known only by its containers."""
    return Stack(
        id=None,
        name=name,
        endpoint_id=endpoint_id,
        type=kind,
        status="active" if running else "inactive",
        managed=False,
    )


# -- containers ---------------------------------------------------------------------------


def _name(raw: dict[str, Any]) -> str:
    names = raw.get("Names")
    if isinstance(names, list) and names:
        return str(names[0]).lstrip("/")
    return str(raw.get("Name", "")).lstrip("/")


def _stack_of(labels: dict[str, Any]) -> tuple[str | None, str | None]:
    project = labels.get(COMPOSE_PROJECT) or labels.get(SWARM_STACK)
    return (str(project) if project else None), (
        str(labels[COMPOSE_SERVICE]) if labels.get(COMPOSE_SERVICE) else None
    )


def container(raw: dict[str, Any]) -> Container:
    labels = raw.get("Labels") or {}
    project, service = _stack_of(labels)
    status_text = str(raw.get("Status", ""))
    health_match = _HEALTH_RE.search(status_text)
    ports = tuple(
        PortMapping(
            private=int(p.get("PrivatePort", 0)),
            proto=str(p.get("Type", "tcp")),
            public=int(p["PublicPort"]) if p.get("PublicPort") else None,
            ip=str(p.get("IP", "") or ""),
        )
        for p in (raw.get("Ports") or [])
        if isinstance(p, dict)
    )
    return Container(
        id=str(raw.get("Id", "")),
        name=_name(raw),
        state=str(raw.get("State", "")),
        status_text=status_text,
        health=health_match.group(1) if health_match else "",
        image=str(raw.get("Image", "")),
        image_id=str(raw.get("ImageID", "")),
        created_at=iso_from_unix(raw.get("Created")),
        ports=ports,
        stack=project,
        service=service,
    )


def container_detail(raw: dict[str, Any]) -> ContainerDetail:
    state = raw.get("State") or {}
    config = raw.get("Config") or {}
    host = raw.get("HostConfig") or {}
    health = state.get("Health") or {}
    labels = config.get("Labels") or {}
    project, service = _stack_of(labels)
    published: list[PortMapping] = []
    for spec, bindings in ((raw.get("NetworkSettings") or {}).get("Ports") or {}).items():
        private, _, proto = str(spec).partition("/")
        if not private.isdigit():
            continue
        if not bindings:
            published.append(PortMapping(private=int(private), proto=proto or "tcp"))
            continue
        for b in bindings:
            published.append(
                PortMapping(
                    private=int(private),
                    proto=proto or "tcp",
                    public=int(b["HostPort"]) if str(b.get("HostPort", "")).isdigit() else None,
                    ip=str(b.get("HostIp", "") or ""),
                )
            )
    base = Container(
        id=str(raw.get("Id", "")),
        name=_name(raw),
        state=str(state.get("Status", "")),
        status_text="",
        health=str(health.get("Status", "")) if health else "",
        image=str(config.get("Image", "")),
        image_id=str(raw.get("Image", "")),
        created_at=iso_clean(raw.get("Created")),
        ports=tuple(published),
        stack=project,
        service=service,
    )
    log_entries = health.get("Log") or []
    return ContainerDetail(
        container=base,
        restart_count=int(raw.get("RestartCount") or 0),
        started_at=iso_clean(state.get("StartedAt")),
        finished_at=iso_clean(state.get("FinishedAt")),
        exit_code=state.get("ExitCode") if state.get("Status") != "running" else None,
        restart_policy=str((host.get("RestartPolicy") or {}).get("Name", "")),
        health_log=tuple(
            str(e.get("Output", "")).strip()[:300] for e in log_entries[-3:] if isinstance(e, dict)
        ),
        mounts=tuple(
            Mount(
                type=str(m.get("Type", "")),
                source=str(m.get("Source", "") or m.get("Name", "")),
                destination=str(m.get("Destination", "")),
                read_only=not m.get("RW", True),
            )
            for m in (raw.get("Mounts") or [])
            if isinstance(m, dict)
        ),
        networks=tuple(sorted((raw.get("NetworkSettings") or {}).get("Networks") or {})),
        tty=bool(config.get("Tty")),
        # "NAME=value" -> NAME. The value is dropped here, before anything can show it.
        env_names=tuple(
            sorted({str(e).split("=", 1)[0] for e in (config.get("Env") or []) if "=" in str(e)})
        ),
    )


# -- logs ---------------------------------------------------------------------------------


def _split_ts(line: str) -> tuple[str | None, str]:
    head, sep, rest = line.partition(" ")
    if sep and _TS_RE.match(head):
        return head, rest
    return None, line


def _lines(text: str, stream: str, timestamps: bool) -> list[LogLine]:
    out: list[LogLine] = []
    for line in text.splitlines():
        ts, body = _split_ts(line) if timestamps else (None, line)
        out.append(LogLine(ts=ts, stream=stream, text=body))
    return out


def parse_log_stream(data: bytes, *, tty: bool, timestamps: bool = True) -> list[LogLine]:
    """Decode Docker's log body. A container without a TTY multiplexes stdout and stderr
    into frames (an 8-byte header: stream, three zeros, big-endian length); with a TTY
    the body is plain text. Anything that does not look like frames is read as text, so a
    surprise never loses the log."""
    if tty or not data:
        return _lines(data.decode("utf-8", "replace"), "stdout", timestamps)
    out: list[LogLine] = []
    pos = 0
    while pos + 8 <= len(data):
        kind = data[pos]
        if kind not in (0, 1, 2) or data[pos + 1 : pos + 4] != b"\x00\x00\x00":
            out.extend(_lines(data[pos:].decode("utf-8", "replace"), "stdout", timestamps))
            return out
        size = int.from_bytes(data[pos + 4 : pos + 8], "big")
        payload = data[pos + 8 : pos + 8 + size]
        out.extend(
            _lines(
                payload.decode("utf-8", "replace"), "stderr" if kind == 2 else "stdout", timestamps
            )
        )
        pos += 8 + size
    return out


class FrameDemux:
    """Incremental decoder for Docker's multiplexed stream (exec output without a TTY): feed it
    chunks as they arrive, get ``(stream, payload)`` pairs back. A body that does not start with
    a valid frame header is passed through as stdout, so a surprise never loses the output."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._plain = False

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        if self._plain:
            return [("stdout", data)] if data else []
        self._buf += data
        out: list[tuple[str, bytes]] = []
        while len(self._buf) >= 8:
            kind = self._buf[0]
            if kind not in (0, 1, 2) or self._buf[1:4] != b"\x00\x00\x00":
                self._plain = True
                out.append(("stdout", bytes(self._buf)))
                self._buf.clear()
                return out
            size = int.from_bytes(self._buf[4:8], "big")
            if len(self._buf) < 8 + size:
                break
            payload = bytes(self._buf[8 : 8 + size])
            del self._buf[: 8 + size]
            out.append(("stderr" if kind == 2 else "stdout", payload))
        return out

    def finish(self) -> list[tuple[str, bytes]]:
        """Whatever is left when the stream ends (an incomplete frame is kept as text)."""
        rest, self._buf = bytes(self._buf), bytearray()
        return [("stdout", rest)] if rest else []


# -- stats --------------------------------------------------------------------------------


def container_stats(raw: dict[str, Any]) -> ContainerStats:
    cpu = raw.get("cpu_stats") or {}
    pre = raw.get("precpu_stats") or {}
    cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (
        pre.get("cpu_usage") or {}
    ).get("total_usage", 0)
    sys_delta = (cpu.get("system_cpu_usage") or 0) - (pre.get("system_cpu_usage") or 0)
    cpus = (
        cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    )
    percent = (cpu_delta / sys_delta) * cpus * 100.0 if sys_delta > 0 and cpu_delta > 0 else 0.0
    mem = raw.get("memory_stats") or {}
    cache = (
        (mem.get("stats") or {}).get("inactive_file") or (mem.get("stats") or {}).get("cache") or 0
    )
    nets = (raw.get("networks") or {}).values()
    return ContainerStats(
        cpu_percent=round(percent, 2),
        mem_bytes=max(0, int(mem.get("usage", 0)) - int(cache)),
        mem_limit=int(mem.get("limit", 0)),
        net_rx_bytes=sum(int(n.get("rx_bytes", 0)) for n in nets),
        net_tx_bytes=sum(int(n.get("tx_bytes", 0)) for n in nets),
        pids=int((raw.get("pids_stats") or {}).get("current", 0)),
    )


# -- image or build -----------------------------------------------------------------------


def classify_compose(text: str) -> str:
    """``image`` when every service pulls an image, ``build`` when every one builds from
    source, ``mixed`` when both, ``unknown`` when it cannot be read. The text is parsed
    and dropped; nothing from it is kept."""
    try:
        doc = YAML(typ="safe").load(text)
    except (YAMLError, ValueError):
        return "unknown"
    services = (doc or {}).get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict) or not services:
        return "unknown"
    builds = images = 0
    for spec in services.values():
        if not isinstance(spec, dict):
            continue
        if spec.get("build"):
            builds += 1  # builds (and may tag an image): the source is what deploys
        elif spec.get("image"):
            images += 1
    if builds and images:
        return "mixed"
    if builds:
        return "build"
    if images:
        return "image"
    return "unknown"


def mask_inspect(raw: dict[str, Any]) -> dict[str, Any]:
    """``docker inspect`` output with every environment value replaced by ``***`` (names kept)."""
    import copy

    out = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    cfg = out.get("Config") or {}
    if isinstance(cfg.get("Env"), list):
        cfg["Env"] = [str(e).split("=", 1)[0] + "=***" for e in cfg["Env"]]
    return out
