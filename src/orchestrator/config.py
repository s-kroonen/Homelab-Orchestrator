"""Application configuration loaded from environment variables.

All settings are 12-factor: env-driven, with sensible defaults for local
development. A ``.env`` file at the repo root is auto-loaded if present.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdapterMode(StrEnum):
    """Which implementation an adapter uses. ``DRY_RUN=true`` forces all to ``dry_run``."""

    LIVE = "live"
    DRY_RUN = "dry_run"


class Settings(BaseSettings):
    """Top-level settings object. Instantiated once at app startup."""

    # Two env files, later wins. `.env` holds deployment config (typically
    # container-shaped: /data, /etc/orchestrator). `.env.local` is an optional
    # developer override for running natively — on Windows or a dev box the
    # container paths don't exist, and this avoids editing the deploy config to
    # test locally. Both are git-ignored.
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Runtime ----------------------------------------------------------
    orchestrator_env: Literal["development", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    dry_run: bool = True

    # Per-adapter selector; strings so config surfaces the intent, but only
    # ``live`` vs ``dry_run`` is honoured today. Live values from the .env
    # (e.g. "mqtt", "api") are treated as LIVE.
    power_adapter: str = "dry_run"
    proxmox_adapter: str = "dry_run"
    pbs_adapter: str = "dry_run"
    notifier_adapter: str = "null"

    # ---- Web / API --------------------------------------------------------
    http_host: str = "0.0.0.0"
    http_port: int = 8080

    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Homelab Orchestrator"
    webauthn_origin: str = "http://localhost:8080"

    session_secret: SecretStr = SecretStr("dev-only-change-me")

    # ---- Persistence ------------------------------------------------------
    database_url: str = "sqlite:///./data/orchestrator.db"
    state_dir: Path = Path("./data")

    nfs_state_dump_path: Path | None = None
    state_dump_interval_minutes: int = 60

    run_migrations_on_start: bool = True

    # ---- Service registry -------------------------------------------------
    services_yaml_path: Path = Path("./config/services.yaml")

    # ---- Proxmox ----------------------------------------------------------
    proxmox_host: str = "proxmox.example.lan"
    proxmox_port: int = 8006
    proxmox_verify_tls: bool = True
    proxmox_token_id: str = ""
    proxmox_token_secret: SecretStr = SecretStr("")
    proxmox_restore_token_id: str = ""
    proxmox_restore_token_secret: SecretStr = SecretStr("")

    # The PVE *storage ID* that points at PBS — this is what `vzdump storage=`
    # wants. It is NOT necessarily the same string as the PBS datastore name
    # below; PVE names the storage entry, PBS names the datastore.
    pve_backup_storage: str = "example-pbs-storage"

    # ---- PBS --------------------------------------------------------------
    pbs_host: str = "pbs.example.lan"
    pbs_port: int = 8007
    pbs_verify_tls: bool = True
    # The PBS *datastore name* — what the PBS API addresses.
    pbs_datastore: str = "example-datastore"
    # Node name PBS runs tasks under; "localhost" on a standalone install.
    pbs_node_name: str = "localhost"
    pbs_token_id: str = ""
    pbs_token_secret: SecretStr = SecretStr("")

    # Snapshot protection (the "known-good pin") needs the PBS privilege
    # Datastore.Modify, which on many installs requires a custom role or the
    # built-in DatastoreAdmin. Set false when the token lacks it: protect calls
    # become logged no-ops instead of 403s.
    # TRADE-OFF: with this off, prune has nothing stopping it from removing the
    # last verified backup of a service. See docs/api_tokens.md.
    pbs_protect_enabled: bool = True

    # ---- Probe transports (phase 4) ---------------------------------------
    # Defaults for SSH probes; a probe's own transport block can override them.
    # In Docker the key and known_hosts must be MOUNTED into the container —
    # existing on the host is not enough.
    ssh_key_path: str = ""
    ssh_known_hosts_path: str = ""
    # Verifying host keys is the default because some probe configs reference
    # credentials on the guest; accepting any key would expose them on the path.
    ssh_verify_host_key: bool = True

    # Default ProxyJump for SSH probes. The orchestrator is typically not on the
    # guests' network, so shell probes hop through the gateway. A probe's own
    # transport block can override or clear it.
    ssh_jump_host: str = ""
    ssh_jump_user: str = ""
    ssh_jump_port: int = 22

    # Host runner: the container asks a process on the HOST to perform checks
    # that need credentials, instead of holding those credentials itself. Only
    # this socket is mounted in — no inventory, no playbooks, no keys.
    # See docs/host_runner.md and contrib/host-runner/.
    host_runner_socket: str = ""

    # ---- Config builder defaults ------------------------------------------
    # Used by `init`, `scaffold` and `probe add` as suggestions. They are written
    # EXPLICITLY into each generated entry and never applied at runtime, so
    # services.yaml always shows the real route a probe takes.
    #: The reverse proxy's LOCAL address, e.g. https://10.0.0.2 — HTTP probes point
    #: here and route with a Host header, like curl --resolve.
    probe_proxy_base_url: str = ""
    #: Slug of the gateway service. New services get `depends_on: [<this>]`, so a
    #: gateway outage reads as UNKNOWN downstream rather than as every service failing.
    gateway_service: str = ""

    # ---- Task timeouts ----------------------------------------------------
    backup_task_timeout_s: int = 7200  # a large VM dump can legitimately take hours
    verify_task_timeout_s: int = 1800
    # A cold node can legitimately take minutes to POST, boot, and bring its
    # guests up — this bounds the whole wake pipeline, node-online and health
    # polling both count against it.
    wake_timeout_s: int = 600
    wake_poll_interval_s: float = 5.0
    # Just the Proxmox "start" task, not the guest finishing its own boot —
    # that's what wake_timeout_s's health polling is for.
    wake_guest_start_timeout_s: int = 120

    # ---- MQTT / power manager --------------------------------------------
    mqtt_host: str = "mqtt.example.lan"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: SecretStr = SecretStr("")
    mqtt_tls: bool = False
    mqtt_power_topic_prefix: str = "power/nodes"

    # ---- Notifier ---------------------------------------------------------
    mail_smtp_host: str = ""
    mail_smtp_port: int = 587
    mail_smtp_username: str = ""
    mail_smtp_password: SecretStr = SecretStr("")
    mail_from: str = ""
    ntfy_url: str = ""
    ntfy_token: SecretStr = SecretStr("")

    # ---- Derived helpers --------------------------------------------------
    @field_validator("state_dir", "services_yaml_path", mode="before")
    @classmethod
    def _expand_path(cls, v: str | Path | None) -> Path | None:
        if v is None or v == "":
            return None
        return Path(v).expanduser() if isinstance(v, str) else v

    def resolve_adapter(self, chosen: str) -> AdapterMode:
        """DRY_RUN=true forces dry_run regardless of the per-adapter value."""
        if self.dry_run:
            return AdapterMode.DRY_RUN
        if chosen.lower() in {"dry_run", "dryrun", "fake", "null"}:
            return AdapterMode.DRY_RUN
        return AdapterMode.LIVE


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton (lazy, cached)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Test helper — clears the cached singleton."""
    global _settings
    _settings = None
