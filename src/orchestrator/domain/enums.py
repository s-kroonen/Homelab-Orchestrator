"""Enums shared across modules. Kept in one place so DB, API, and UI agree."""

from __future__ import annotations

from enum import StrEnum


class HealthState(StrEnum):
    """Three-state health model — drives the backup gate and maintenance page.

    HEALTHY -- Proxmox boot ok AND all *required* probes pass. Only state that backs up.
    FAILED  -- A definitive negative (unit failed, container exited, HTTP 5xx).
               Skip backup, alert, mark as a restore candidate.
    UNKNOWN -- Indeterminate (probe timed out, node unreachable). Fail closed:
               skip backup, alert only, no restore action taken automatically.
    """

    HEALTHY = "healthy"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PowerState(StrEnum):
    ON = "on"
    OFF = "off"
    BOOTING = "booting"
    STOPPING = "stopping"
    UNKNOWN = "unknown"


class GuestKind(StrEnum):
    """Kind of Proxmox guest a service maps to."""

    VM = "vm"
    CT = "ct"  # LXC / Incus container
    NONE = "none"  # e.g. a bare-metal-ish workload, or purely observational


class ProbeKind(StrEnum):
    """Built-in probe kinds. Add new kinds by registering an implementation in
    ``orchestrator.health.probes`` and extending this enum."""

    HTTP = "http"  # GET/HEAD an endpoint, assert status class
    TCP = "tcp"  # bare socket connect
    SYSTEMD = "systemd"  # ssh + `systemctl is-active`
    MQTT = "mqtt"  # observe a topic within a window
    DOCKER_PROJECT = "docker_project"  # all containers in a compose project healthy
    DB_REDIS = "db_redis"  # BGSAVE + redis-check-rdb on the dump
    DB_MONGO = "db_mongo"  # mongodump validate (WiredTiger)
    DB_SQLITE = "db_sqlite"  # PRAGMA integrity_check on a copy
    DB_MARIADB = "db_mariadb"  # mariadb-check + single-transaction dump
    CUSTOM_SCRIPT = "custom_script"  # execute a user-provided script; exit 0 = ok


class PipelineKind(StrEnum):
    WAKE = "wake"
    BACKUP = "backup"
    RESTORE = "restore"
    SCAN = "scan"


class PipelineStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"  # e.g. backup skipped because gate returned FAILED/UNKNOWN
    CANCELLED = "cancelled"


class BackupMode(StrEnum):
    SNAPSHOT = "snapshot"
    SUSPEND = "suspend"
    STOP = "stop"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ApprovalKind(StrEnum):
    REVERT = "revert"  # restore a service to a PBS snapshot
    GREENLIGHT = "greenlight"  # bless an unattended restore-to-new-vmid
    POWER_OFF = "power_off"
    CONFIG_CHANGE = "config_change"


class RouterProvider(StrEnum):
    TRAEFIK = "traefik"
    PANGOLIN = "pangolin"
    NPM = "npm"
    OTHER = "other"


class AuditResult(StrEnum):
    OK = "ok"
    DENIED = "denied"
    ERROR = "error"
