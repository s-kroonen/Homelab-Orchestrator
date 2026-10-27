"""Application configuration loaded from environment variables.

All settings are 12-factor: env-driven, with sensible defaults for local
development. A ``.env`` file at the repo root is auto-loaded if present.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
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

    # Run BackupPolicy schedules (phase 6). Off = no cron jobs at all; manual
    # backups via the CLI/API still work. Independent of DRY_RUN: with both on,
    # scheduled runs fire and exercise the pipeline against log-only adapters.
    scheduler_enabled: bool = True

    # Per-adapter selector; strings so config surfaces the intent, but only
    # ``live`` vs ``dry_run`` is honoured today. Live values from the .env
    # (e.g. "mqtt", "api") are treated as LIVE.
    power_adapter: str = "dry_run"
    proxmox_adapter: str = "dry_run"
    pbs_adapter: str = "dry_run"
    proxy_adapter: str = "dry_run"
    notifier_adapter: str = "null"

    # IANA timezone times are shown in and backup cron schedules run in (e.g.
    # Europe/Amsterdam). Blank = the container's TZ, else UTC. Can be changed from the
    # System page without a restart; this is only the default until someone does.
    timezone: str = ""

    # ---- Web / API --------------------------------------------------------
    http_host: str = "0.0.0.0"
    http_port: int = 8080

    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Homelab Orchestrator"
    webauthn_origin: str = "http://localhost:8080"

    session_secret: SecretStr = SecretStr("dev-only-change-me")

    # How long an admin-invite link (generated from the dashboard, used to add
    # another admin's passkey) stays valid before it must be re-issued.
    invite_ttl_hours: int = 24

    # Directory holding .env and config/ that the dashboard's /setup page may
    # write — the web equivalent of `orchestrator-cli init --dir`. None (the
    # default) disables /setup entirely: the orchestrator container normally
    # receives .env as environment variables and has no writable copy of it.
    # Set it via compose `environment:`, NOT in .env — a non-empty .env already
    # counts as "configured" and closes the page (same rule as `init`).
    setup_project_dir: Path | None = None

    # ---- Persistence ------------------------------------------------------
    database_url: str = "sqlite:///./data/orchestrator.db"
    state_dir: Path = Path("./data")

    nfs_state_dump_path: Path | None = None
    state_dump_interval_minutes: int = 60

    run_migrations_on_start: bool = True

    # ---- The orchestrator managing itself (docs/system.md) ----------------
    # Scheduled backups of the database + services.yaml into state_dir/backups.
    self_backup_enabled: bool = True
    self_backup_interval_hours: int = 24
    self_backup_keep: int = 14
    # Update from the dashboard: a command run on the Docker HOST through a
    # transport (normally the host runner), e.g.
    #   SELF_UPDATE_TRANSPORT={"type": "host_agent", "host": "docker-host"}
    #   SELF_UPDATE_COMMAND=sh -c '(sleep 3; cd /opt/orchestrator && docker compose pull orchestrator && docker compose up -d orchestrator) >/tmp/orchestrator-update.log 2>&1 &'
    # Blank = the page shows the commands to run by hand instead.
    self_update_transport: str = ""
    self_update_command: str = ""
    self_update_timeout_s: int = 120
    # Which GitHub repo / branch "is there a newer build?" looks at. The branch
    # defaults to the tag of ORCHESTRATOR_IMAGE (dev or main).
    update_repo: str = "s-kroonen/homelab-orchestrator"
    update_branch: str = ""
    # Baked in by the image build (Dockerfile ARG GIT_SHA) and passed by compose.
    orchestrator_git_sha: str = ""
    orchestrator_image: str = ""

    # ---- Service registry -------------------------------------------------
    services_yaml_path: Path = Path("./config/services.yaml")
    #: Where the connections live (blank: ``connections.yaml`` in STATE_DIR). See docs/connections.md.
    connections_path: Path | None = None
    #: This orchestrator's own site name ("home", "offsite"), for telling its connections from
    #: another orchestrator's once two of them run side by side.
    orchestrator_site: str = ""

    # ---- Health scanning (periodic) ----------------------------------------
    # Without this, ServiceState is only ever updated as a SIDE EFFECT of a
    # wake or a backup's gate check (see HealthScanner's module docstring) —
    # which means two services sharing a dependency can show mutually
    # inconsistent verdicts for it, because each one re-probed it live at a
    # different moment. This runs one scan cycle on its own schedule instead.
    health_scan_enabled: bool = True
    # 10 minutes: frequent enough that the dashboard is never stale for long,
    # infrequent enough not to hammer a low-power homelab's hosts.
    health_scan_interval_s: int = 600
    # Deliberately short — this is a monitoring trend view, not an audit
    # trail (AuditEntry/PipelineRun already serve that purpose for backups
    # and wakes), so it is kept small on purpose.
    health_history_retention_days: int = 7

    # ---- Service IP discovery ----------------------------------------------
    # Periodically find each running guest's IP: QEMU guest agent / LXC config
    # + interfaces first (Proxmox API), then the guest's NIC MACs looked up in
    # an ARP table and a DHCP leases file. A service's manual `ip_address`
    # always wins over whatever is found. See docs/service_ips.md.
    ip_discovery_enabled: bool = True
    ip_discovery_interval_s: int = 600
    # Where the ARP / DHCP reads run — a transport spec in the same shape as a
    # probe's `transport:` block, as JSON, e.g.
    #   {"type": "host_agent", "host": "gateway"}
    #   {"type": "ssh", "host": "10.0.0.1", "user": "orchestrator"}
    # A bare word ("local", "host_agent") is shorthand for {"type": <word>}.
    # Empty = no ARP / DHCP fallback at all (Proxmox-only discovery). The
    # container's own ARP table only sees its Docker bridge, so "local" is
    # rarely what you want unless the container runs with network_mode: host.
    ip_discovery_transport: str = ""
    # Read the neighbour (ARP) table over the transport. Needs the transport.
    ip_discovery_arp_enabled: bool = True
    # Comma-separated CIDRs to "ARP ping" before reading the table, so guests
    # that have not talked to the transport host lately are in it (fping -a -q
    # -g if present, else a bounded parallel ping loop). Max /22 per CIDR.
    # Empty = no sweep, just read the table.
    ip_discovery_sweep_cidrs: str = ""
    # dnsmasq-format leases file on the transport host (`expiry mac ip host
    # clientid` per line), e.g. /var/lib/misc/dnsmasq.leases. Empty = no DHCP
    # fallback.
    ip_discovery_dhcp_leases_path: str = ""

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

    # ---- Traefik ------------------------------------------------------------
    # Read-only drift detection: do the ``proxy_host`` records in the DB have a
    # matching live router in Traefik? Empty = disabled, no polling attempted —
    # this is entirely optional, same as PBS/Proxmox degrading when unset.
    # Requires Traefik started with --api=true; see docs/proxy_hosts.md.
    # Example: http://traefik.example.lan:8080
    traefik_api_url: str = ""
    traefik_api_verify_tls: bool = True
    # Traefik's API has no auth of its own — it's normally protected by network
    # ACLs or a middleware. Set BOTH to enable HTTP basic auth in front of it;
    # either alone is ignored. There is no bearer-token variant: Traefik has no
    # built-in way to check one, so adding that option would just invite a
    # false sense of security.
    traefik_api_username: str = ""
    traefik_api_password: SecretStr = SecretStr("")
    traefik_api_timeout_s: float = 5.0

    # Traffic metrics (Prometheus text format at ``{traefik_api_url}/metrics``,
    # requires Traefik started with --metrics.prometheus=true). Gated by the
    # same ``traefik_api_url`` being non-empty — no separate on/off flag, same
    # as everything else Traefik-related. See docs/traffic_metrics.md.
    traefik_metrics_poll_interval_s: int = 60
    traefik_metrics_retention_days: int = 14

    # ---- Traefik Manager (write path) --------------------------------------
    # Traefik Manager (https://github.com/chr0nzz/traefik-manager) is now the
    # source of truth for proxy routes on the gateway VM — it manages the same
    # one-file-per-service /etc/traefik/dynamic/ layout docs/gateway_traefik_
    # migration.md describes, and the orchestrator talks to it as a CLIENT,
    # the same relationship it already has with Proxmox/IPMI. Unlike
    # TRAEFIK_API_URL above (Traefik's own API, read-only, never written to),
    # this integration DOES write: the maintenance-page middleware injection
    # in docs/proxy_hosts.md. Empty = disabled entirely, same "empty disables
    # it" convention as every other optional integration in this app.
    traefik_manager_url: str = ""
    # Generated per-device in Traefik Manager's own UI (Settings ->
    # Authentication -> API Keys). Sent as the X-Api-Key header; API-key auth
    # bypasses Traefik Manager's CSRF checks entirely, so no CSRF token
    # handling is needed here.
    traefik_manager_api_key: SecretStr = SecretStr("")
    traefik_manager_verify_tls: bool = True
    # Short on purpose, same reasoning as TRAEFIK_API_TIMEOUT_S: these calls
    # run synchronously from a dashboard-triggered proxy-host mutation, not a
    # background job.
    traefik_manager_timeout_s: float = 5.0
    # Pre-selected entry points / certificate resolver for a NEW live-managed
    # proxy host (comma list; blank = pick from the live Traefik: `websecure`
    # if it exists, else the first entry point that is not `traefik`). Only a
    # pre-selection - the operator's choice is what gets written, never a
    # blind default. See docs/proxy_hosts.md.
    traefik_default_entrypoints: str = ""
    traefik_default_cert_resolver: str = ""
    # Name of the HTTP middleware that redirects plain HTTP to HTTPS - the
    # "Redirect HTTP to HTTPS" option on a proxy host puts a companion router
    # on the plain-HTTP entry point with this middleware. Must already exist
    # in Traefik (e.g. a shared ``redirect-to-https`` defined once).
    traefik_redirect_middleware: str = "redirect-to-https"
    # Name of the headers middleware (stsSeconds...) that a proxy host's "HSTS" option
    # attaches to its route. Must already exist in Traefik, like the redirect one.
    traefik_hsts_middleware: str = "hsts-headers"
    # Which Traefik Manager config file a NEW route is created in. A template:
    # ``{service}`` = the linked service's slug, ``{hostname}`` = the hostname
    # with dots turned into dashes. The default keeps one file per service
    # (``media.yml``), leaving ``_shared.yml`` for shared middlewares. Blank =
    # Traefik Manager's own default file. A host can override it
    # (``route_options.config_file``). An existing route stays in the file it is
    # in until it is explicitly moved. See docs/proxy_hosts.md.
    traefik_manager_config_file: str = "{service}.yml"

    # The orchestrator's own base URL, as REACHABLE FROM THE GATEWAY VM (e.g.
    # http://10.0.0.5:8000) — not necessarily the same as anything a browser
    # uses. This is the backend target for the maintenance-page fallback
    # service Traefik's `errors` middleware calls when a service's backend is
    # down (see docs/proxy_hosts.md's "Maintenance-page fallback" section).
    # Empty = the whole maintenance-middleware-injection feature is a no-op,
    # same convention as PROBE_PROXY_BASE_URL above.
    orchestrator_public_url: str = ""

    # ---- GeoIP traffic visibility (opt-in) ---------------------------------
    # Per-request client-IP/country visibility, sourced from Traefik's own
    # access log TAILED THROUGH TRAEFIK MANAGER (ProxyAdapter.tail_access_log,
    # GET /api/traefik/logs?lines=N) — never direct SSH/filesystem access on
    # this app's side. Gated on this flag AND on Traefik Manager's own
    # ACCESS_LOG_PATH being configured on the gateway (tail_access_log
    # degrades to None otherwise, same convention as list_routes). Off by
    # default: this is a new, optional integration, same "empty/false
    # disables it" convention as every other one in this app. See
    # docs/geoip_traffic.md. Gates only the GeoIP half (samples, database,
    # /api/traffic/geo); the access-log tail itself is ACCESS_LOG_ENABLED.
    geoip_enabled: bool = False
    # Path to a MaxMind-format (.mmdb) GeoIP database. Empty = auto-download
    # and cache the free DB-IP city-lite database under
    # ``{STATE_DIR}/geoip/`` — the same approach Traefik Manager's own docs
    # describe for its own GeoIP feature (POST /api/geoip/update), rather
    # than inventing a different one. A configured path that does not exist
    # is logged and treated as "GeoIP unavailable" (never auto-downloaded
    # over an explicit operator choice).
    geoip_db_path: str = ""
    # DEPRECATED aliases for ACCESS_LOG_POLL_INTERVAL_S / ACCESS_LOG_LINES
    # (the GeoIP collector became the shared access-log collector). Honoured
    # only when the new name is not set; empty = unused.
    geoip_traffic_poll_interval_s: int | None = None
    geoip_access_log_lines: int | None = None
    # Deliberately short, same reasoning as HEALTH_HISTORY_RETENTION_DAYS /
    # TRAEFIK_METRICS_RETENTION_DAYS: this is a monitoring trend view, not an
    # audit trail.
    geoip_traffic_retention_days: int = 14

    # ---- Access-log ingestion (powers /traffic and /traffic/geo) -----------
    # Traefik's access log, tailed through Traefik Manager (GET
    # /api/traefik/logs). Runs whenever the proxy adapter is a live Traefik
    # Manager; TRAEFIK_API_URL is NOT needed. GeoIP samples are additionally
    # written only when GEOIP_ENABLED. See docs/traffic_metrics.md.
    access_log_enabled: bool = True
    access_log_poll_interval_s: int = 30
    # Lines requested per poll; Traefik Manager caps this at 1000.
    access_log_lines: int = 1000
    # Per-minute traffic_rollup rows older than this are pruned each poll.
    traffic_rollup_retention_days: int = 14

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
    # A scheduled backup that was blocked or whose target did not become healthy
    # after boot is tried again this many seconds later, up to the policy's
    # max_attempts (default 3), then it gives up and notifies.
    backup_retry_delay_s: int = 300
    # After a backup, wait this long before powering a node it woke back off, so a
    # request that arrives right at the end is not pulled out from under.
    backup_power_off_grace_s: int = 30
    # A cold node can legitimately take minutes to POST, boot, and bring its
    # guests up — this bounds the whole wake pipeline, node-online and health
    # polling both count against it.
    wake_timeout_s: int = 600
    wake_poll_interval_s: float = 5.0
    # Just the Proxmox "start" task, not the guest finishing its own boot —
    # that's what wake_timeout_s's health polling is for.
    wake_guest_start_timeout_s: int = 120
    # After a wake run ends (success or failure), the next hit to /wake or the
    # maintenance page reuses it rather than starting a fresh one. Without
    # this, a wake that fails fast gets re-triggered on literally every
    # incoming request during an outage — every visitor, not just page
    # reloads, since Traefik's errors middleware routes every one of them here.
    wake_retry_cooldown_s: int = 30

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
    #: Where e-mail alerts go (comma-separated). Needs MAIL_SMTP_HOST and MAIL_FROM.
    mail_to: str = ""
    mail_smtp_starttls: bool = True
    #: Any HTTP endpoint that takes a JSON POST (Home Assistant, Slack bridge, ...).
    notify_webhook_url: str = ""
    notify_webhook_token: SecretStr = SecretStr("")
    # ---- Automatic recovery (docs/recovery.md) -------------------------------
    #: Global kill switch; each service also opts in with its own `recovery:` block.
    recovery_enabled: bool = True
    #: A service must be FAILED on this many consecutive scans before a recovery attempt.
    recovery_confirm_scans: int = 2
    #: How long a restore approval link stays valid.
    approval_ttl_hours: int = 24
    #: How admins reach this dashboard from their phone/browser, for the links in alerts
    #: (falls back to ORCHESTRATOR_PUBLIC_URL, which is the gateway-side address).
    external_url: str = ""
    # ---- Dumps: a database backed up on its own (docs/dumps.md) --------------------
    #: Where dump files are kept; blank = <STATE_DIR>/dumps. Point it at a mount (NFS) to keep
    #: them off this host.
    dump_dir: str = ""

    # ---- MCP server: an AI assistant's eye on the server space (docs/mcp.md) -----
    #: Serve /mcp and its OAuth endpoints. false = they answer 404.
    mcp_enabled: bool = True
    #: Lifetime of an access token; the client refreshes it with its refresh token.
    mcp_access_ttl_s: int = 3600
    #: A connection nobody used for this long stops working (each refresh extends it).
    mcp_refresh_ttl_days: int = 90
    #: How long a request that needs a human stays open for approval.
    mcp_approval_ttl_min: int = 60
    #: Hosts whose Client ID Metadata Documents are fetched (comma-separated). Claude's own.
    mcp_cimd_hosts: str = "claude.ai,claude.com"
    # ---- Controllers: acting on one service itself (docs/controllers.md) ------
    portainer_url: str = ""
    portainer_api_key: SecretStr = SecretStr("")
    portainer_verify_tls: bool = True
    # api | dry_run. dry_run (or DRY_RUN=true) serves sample stacks and containers.
    portainer_adapter: str = "api"
    # true: the orchestrator may only READ Portainer (no start, stop, redeploy, create or
    # delete). A guard rail while you are still getting to trust it.
    portainer_read_only: bool = False
    # The address people open in a browser, for "Open in Portainer" links (blank: PORTAINER_URL).
    portainer_public_url: str = ""
    haos_url: str = ""
    haos_token: SecretStr = SecretStr("")
    haos_verify_tls: bool = True
    controller_timeout_s: int = 20
    # Contact the browsers' push services see with each Web Push (a mailto: or https
    # URL). Blank = the dashboard's own address when it is https, else a placeholder.
    push_vapid_subject: str = ""

    # ---- Notification engine (docs/notifications.md) -----------------------
    notifications_enabled: bool = True
    #: A state change must be seen on this many consecutive scans before it is announced.
    notify_confirm_scans: int = 2
    #: This many state changes inside the window = "flapping": one alert, then quiet.
    notify_flap_threshold: int = 4
    notify_flap_window_s: int = 1800
    notify_retention_days: int = 30
    #: A deleted service stays in the archive (restorable) this many days, then is purged.
    archive_retention_days: int = 30

    # ---- Derived helpers --------------------------------------------------
    @field_validator(
        "state_dir", "services_yaml_path", "setup_project_dir", "connections_path", mode="before"
    )
    @classmethod
    def _expand_path(cls, v: str | Path | None) -> Path | None:
        if v is None or v == "":
            return None
        return Path(v).expanduser() if isinstance(v, str) else v

    @field_validator("traefik_manager_config_file")
    @classmethod
    def _check_config_file_template(cls, v: str) -> str:
        from orchestrator.registry.config_files import check_template

        return check_template(v)

    @field_validator("geoip_traffic_poll_interval_s", "geoip_access_log_lines", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _apply_deprecated_access_log_aliases(self) -> Settings:
        """GEOIP_TRAFFIC_POLL_INTERVAL_S / GEOIP_ACCESS_LOG_LINES still work
        when the new ACCESS_LOG_* name is not set explicitly."""
        fields_set = self.model_fields_set
        if (
            "access_log_poll_interval_s" not in fields_set
            and self.geoip_traffic_poll_interval_s is not None
        ):
            self.access_log_poll_interval_s = self.geoip_traffic_poll_interval_s
        if "access_log_lines" not in fields_set and self.geoip_access_log_lines is not None:
            self.access_log_lines = self.geoip_access_log_lines
        return self

    @property
    def default_entry_points_list(self) -> list[str]:
        """``TRAEFIK_DEFAULT_ENTRYPOINTS`` split on commas, blanks dropped."""
        return [p.strip() for p in self.traefik_default_entrypoints.split(",") if p.strip()]

    def resolve_adapter(self, chosen: str) -> AdapterMode:
        """DRY_RUN=true forces dry_run regardless of the per-adapter value."""
        if self.dry_run:
            return AdapterMode.DRY_RUN
        if chosen.lower() in {"dry_run", "dryrun", "fake", "null"}:
            return AdapterMode.DRY_RUN
        return AdapterMode.LIVE


_settings: Settings | None = None


def _load_settings() -> Settings:
    """The environment's settings, with the connections file (``connections.yaml``) applied on
    top: everything the dashboard manages about adapters comes from there."""
    settings = Settings()
    try:
        from orchestrator.connections.runtime import apply_to

        apply_to(settings)
    except Exception as exc:  # a bad connections file must never keep the app from starting
        import logging

        logging.getLogger(__name__).error("connections could not be applied: %s", exc)
    return settings


def get_settings() -> Settings:
    """Return the process-wide settings singleton (lazy, cached)."""
    global _settings
    if _settings is None:
        _settings = _load_settings()
    return _settings


def reload_settings() -> Settings:
    """Read the environment and the connections file again and update the singleton IN PLACE, so
    everything holding it (schedulers, collectors, adapters) sees the new values at once."""
    current = get_settings()
    fresh = _load_settings()
    for name in Settings.model_fields:
        setattr(current, name, getattr(fresh, name))
    try:  # the connections that were applied to `fresh` are in force for the singleton
        from orchestrator.connections.runtime import current as snapshot

        snapshot().owner = current
    except Exception:  # pragma: no cover - defensive
        pass
    return current


def reset_settings_cache() -> None:
    """Test helper — clears the cached singleton."""
    global _settings
    _settings = None
