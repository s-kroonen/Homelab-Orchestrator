"""Pydantic DTOs shared across modules.

These are transport / config shapes — distinct from ORM rows (``db/models.py``).
Probe configs are discriminated at the app layer so the DB can keep them as
opaque JSON while callers still get typed access.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.domain.enums import HealthState, ProbeKind

# ---------------------------------------------------------------------------
# Probe configs — one Pydantic model per ProbeKind. All share ``kind`` as the
# discriminator so ``ProbeConfig`` can be a tagged union.
# ---------------------------------------------------------------------------


class _ProbeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HttpProbeConfig(_ProbeBase):
    """An HTTP check.

    ``host_header`` exists for the common homelab shape where the orchestrator
    can only reach a service *through* a reverse proxy, and must do so by the
    proxy's LOCAL address rather than its public DNS name (public names resolve
    outward and depend on hairpin NAT and external DNS being healthy — neither of
    which this check is meant to test).

    That is exactly what ``curl --resolve`` does:

        url:         https://192.168.1.10/health   <- the proxy's local IP
        host_header: media.example.com             <- what the proxy routes on

    ``sni_hostname`` defaults to ``host_header`` so a real certificate still
    validates; without it, TLS would be negotiated against the bare IP and fail.
    """

    kind: Literal[ProbeKind.HTTP] = ProbeKind.HTTP
    url: str
    method: Literal["GET", "HEAD"] = "GET"
    #: Status codes (``200``) or whole classes (``"2xx"``) that count as healthy.
    expect_status: list[int | str] = Field(default_factory=lambda: list[int | str]([200]))
    expect_body_contains: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    verify_tls: bool = True

    #: Sent as the ``Host:`` header so a reverse proxy routes to the right
    #: backend even though the URL names an IP.
    host_header: str | None = None
    #: TLS SNI to present. Defaults to ``host_header`` when that is set.
    sni_hostname: str | None = None

    def effective_sni(self) -> str | None:
        return self.sni_hostname or self.host_header

    @field_validator("expect_status")
    @classmethod
    def _statuses(cls, value: list[int | str]) -> list[int | str]:
        out: list[int | str] = []
        for item in value:
            if isinstance(item, str) and item.isdigit():
                item = int(item)
            if isinstance(item, int) and 100 <= item <= 599:
                out.append(item)
            elif (
                isinstance(item, str)
                and len(item) == 3
                and item[0] in "12345"
                and item[1:].lower() == "xx"
            ):
                out.append(item.lower())
            else:
                raise ValueError(f"{item!r} is not a status code (200) or class (2xx)")
        if not out:
            raise ValueError("expect_status needs at least one status code or class")
        return out

    def accepts(self, status: int) -> bool:
        """``status`` is one of the codes, or in one of the classes."""
        return any(
            status == want if isinstance(want, int) else str(status)[0] == want[0]
            for want in self.expect_status
        )


class TcpProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.TCP] = ProbeKind.TCP
    host: str
    port: int


class SystemdProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.SYSTEMD] = ProbeKind.SYSTEMD
    ssh_host: str
    ssh_user: str = "root"
    unit: str


class MqttProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.MQTT] = ProbeKind.MQTT
    topic: str
    within_seconds: int = 30


class DockerProjectProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.DOCKER_PROJECT] = ProbeKind.DOCKER_PROJECT
    ssh_host: str
    ssh_user: str = "root"
    project: str
    compose_file: str | None = None


class DbRedisProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.DB_REDIS] = ProbeKind.DB_REDIS
    ssh_host: str
    ssh_user: str = "root"
    dump_path: str = "/var/lib/redis/dump.rdb"
    trigger_bgsave: bool = True


class DbMongoProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.DB_MONGO] = ProbeKind.DB_MONGO
    ssh_host: str
    ssh_user: str = "root"
    mongodump_uri: str  # e.g. mongodb://user:pass@localhost:27017
    validate_db: str | None = None


class DbSqliteProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.DB_SQLITE] = ProbeKind.DB_SQLITE
    ssh_host: str
    ssh_user: str = "root"
    db_path: str


class DbMariadbProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.DB_MARIADB] = ProbeKind.DB_MARIADB
    ssh_host: str
    ssh_user: str = "root"
    mysql_defaults_file: str  # path to a defaults-extra-file with creds
    databases: list[str] = Field(default_factory=list)  # empty = all


class CustomScriptProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.CUSTOM_SCRIPT] = ProbeKind.CUSTOM_SCRIPT
    ssh_host: str | None = None  # None => run locally on the Pi
    ssh_user: str = "root"
    executable: str  # absolute path to the script
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # Exit 0 is a pass. Non-zero is a fail. Non-existent/permission => UNKNOWN.


class PingProbeConfig(_ProbeBase):
    """ICMP echo. ``host`` blank = the service's effective IP (manual, else
    discovered — see docs/service_ips.md), resolved when the probe runs."""

    kind: Literal[ProbeKind.PING] = ProbeKind.PING
    host: str = ""
    count: int = Field(default=1, ge=1, le=10)
    #: Optional — omitted means ping from the orchestrator container itself.
    transport: dict[str, Any] | None = None


ProbeConfig = Annotated[
    HttpProbeConfig
    | TcpProbeConfig
    | SystemdProbeConfig
    | MqttProbeConfig
    | DockerProjectProbeConfig
    | DbRedisProbeConfig
    | DbMongoProbeConfig
    | DbSqliteProbeConfig
    | DbMariadbProbeConfig
    | CustomScriptProbeConfig
    | PingProbeConfig,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Probe results
# ---------------------------------------------------------------------------


#: ``ProbeResult.kind`` of the synthetic guest-gate result the engine records
#: first in every verdict for a guest-backed service (see
#: ``orchestrator.health.guest_gate``). Deliberately NOT a ``ProbeKind``: it is
#: not something an operator can configure as a probe.
GUEST_CHECK_KIND: Literal["guest"] = "guest"


class ProbeResult(BaseModel):
    """One probe execution's outcome; stored in ``ServiceState.last_probe_results``."""

    model_config = ConfigDict(extra="forbid")

    probe_name: str
    kind: ProbeKind | Literal["guest"]
    state: HealthState
    latency_ms: int | None = None
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ServiceVerdict(BaseModel):
    """Aggregate verdict across a service's required probes."""

    model_config = ConfigDict(extra="forbid")

    state: HealthState
    reason: str
    probe_results: list[ProbeResult]
