"""Pydantic DTOs shared across modules.

These are transport / config shapes — distinct from ORM rows (``db/models.py``).
Probe configs are discriminated at the app layer so the DB can keep them as
opaque JSON while callers still get typed access.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.domain.enums import HealthState, ProbeKind

# ---------------------------------------------------------------------------
# Probe configs — one Pydantic model per ProbeKind. All share ``kind`` as the
# discriminator so ``ProbeConfig`` can be a tagged union.
# ---------------------------------------------------------------------------


class _ProbeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HttpProbeConfig(_ProbeBase):
    kind: Literal[ProbeKind.HTTP] = ProbeKind.HTTP
    url: str
    method: Literal["GET", "HEAD"] = "GET"
    expect_status: list[int] = Field(default_factory=lambda: [200])
    expect_body_contains: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    verify_tls: bool = True


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
    | CustomScriptProbeConfig,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Probe results
# ---------------------------------------------------------------------------


class ProbeResult(BaseModel):
    """One probe execution's outcome; stored in ``ServiceState.last_probe_results``."""

    model_config = ConfigDict(extra="forbid")

    probe_name: str
    kind: ProbeKind
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
