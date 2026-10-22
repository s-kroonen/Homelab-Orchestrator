"""SQLModel table definitions — the state store for the orchestrator.

Design rules for this schema:

* **YAML-owned vs DB-owned.**  The registry tables (:class:`Node`, :class:`Service`,
  :class:`Probe`, :class:`BackupPolicy`, :class:`ProxyHost`) are reconciled from the
  YAML file on every boot.  Admins + passkeys + all runtime tables are DB-only,
  otherwise a reload would lock the operator out and destroy history.
* **JSON columns** hold discriminated-union / bag-of-fields data (probe configs,
  step timings, approval payloads).  Validation is enforced at the Pydantic
  layer, not by SQLite.
* **FK ON DELETE policies** protect history: removing a service from YAML must
  not orphan valid backup rows.  Runtime state (``ServiceState``) is CASCADE.
* **Timestamps** are timezone-aware UTC via ``datetime.now(UTC)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, ForeignKey, String, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

from orchestrator.domain.enums import (
    ApprovalKind,
    ApprovalStatus,
    AuditResult,
    BackupGate,
    BackupMode,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
    PowerState,
    ProbeKind,
    RouterProvider,
    WakeTrigger,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Registry — YAML-owned. Reconciled from services.yaml on boot.
# ---------------------------------------------------------------------------


class Node(SQLModel, table=True):
    """A Proxmox host. ``power_mgr_target`` is opaque to us; the power adapter
    interprets it (an MQTT topic suffix, a hostname, an IPMI target, whatever)."""

    __tablename__ = "node"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    always_on: bool = Field(default=False)
    power_mgr_target: str = ""
    notes: str = ""
    # Where THIS node's own Proxmox API answers ("10.0.0.5" or "10.0.0.5:8006").
    # Used to health-check the node directly. Blank = not known yet: fill it from
    # the cluster listing ("Fill from Proxmox"), or by hand. The node the setup
    # talks to (PROXMOX_HOST) is filled from the setup.
    address: str = ""

    # Top-level safety rail for the unattended, request-triggered wakes: the
    # TrafficCollector-driven "wake on hit" path (see ProxyHost.wake_on_hit)
    # AND the maintenance page Traefik's errors middleware serves, which
    # shows "not available" instead of starting anything while this is off.
    # NOT a blanket wake permission: the manual dashboard button, the CLI wake
    # command and POST /wake/{slug} are unaffected and stay unconditional. Defaults True: it is
    # a rail on top of the per-proxy-host opt-in (which itself defaults
    # False), not an independent opt-in, so leaving it at its default must
    # not silently disable a wake-on-hit an operator deliberately turned on
    # at the proxy-host level.
    wake_on_hit_enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class BackupPolicy(SQLModel, table=True):
    __tablename__ = "backup_policy"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    schedule_cron: str = ""
    mode: BackupMode = Field(default=BackupMode.SNAPSHOT)
    # retention: {"keep_daily": 7, "keep_monthly": 6, "protect_latest_verified": true}
    retention: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # targets: {"services": ["example-media", "example-cloud"]}
    targets: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Slugs of services that must be UP before ANY backup under this policy can
    # work at all — canonically the storage VM hosting the NFS the PBS datastore
    # lives on. Woken first, held until the backup finishes. NOT transitive and
    # NOT a health-gate dependency (that is Service.depends_on). See
    # docs/backup_wake.md.
    requires: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Wake the services this policy backs up (node on, guest running, probes
    # healthy) before their backup starts. Default on: a burst node that is off
    # would otherwise just fail the run.
    wake_before_backup: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Service(SQLModel, table=True):
    __tablename__ = "service"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    name: str
    description: str = ""
    guest_kind: GuestKind = Field(default=GuestKind.NONE)
    guest_id: int | None = None
    enabled: bool = Field(default=True)

    # Circular-backup guard (spec section 4). Distinct from `enabled`:
    #   enabled=False        -> do not manage this service at all
    #   backup_excluded=True -> manage it (wake, health, dashboard) but NEVER
    #                           back it up, not even on a manual trigger.
    # The motivating case is the TrueNAS VM: it hosts the ZFS pool that PBS
    # itself lives on, so dumping it to PBS writes the backup into the thing
    # being backed up. There is deliberately no override flag — that is never
    # a correct operation, so it should not be one keystroke away.
    backup_excluded: bool = Field(default=False)
    backup_excluded_reason: str = ""

    # Second safety rail for the TrafficCollector-driven "wake on hit" path
    # — see ProxyHost.wake_on_hit and Node.wake_on_hit_enabled for the full
    # three-way gate. Same reasoning as Node.wake_on_hit_enabled: this is a
    # rail on top of the per-proxy-host opt-in, not an opt-in itself, so it
    # defaults True so that turning on wake_on_hit for a proxy host is
    # enough by itself (the common case) without also having to flip a
    # second switch on the service. An operator who wants this service
    # specifically exempted (even though its proxy host opts in) flips this
    # one off.
    wake_on_hit_enabled: bool = Field(default=True)

    # HOW a request wakes this service when wake_on_hit_enabled is on. CALL: the
    # maintenance page (and the traffic collector) start it on any request.
    # BUTTON: the page shows a Start button and only a press starts it, so a
    # scraper, a monitor or an app polling the URL never wakes it by accident;
    # the traffic collector leaves BUTTON services alone for the same reason.
    wake_trigger: WakeTrigger = Field(default=WakeTrigger.CALL)

    # Free-form organisational tag — purely cosmetic. "" means ungrouped, not a
    # distinct "Ungrouped" group. Lets the dashboard collapse the service table
    # and offer bulk target selection once a registry grows past a handful of
    # services; it has no effect on backups, proxy routing or health.
    group: str = ""

    # Slugs of services that must be HEALTHY before this one can be judged at
    # all. The motivating case: the orchestrator is not on the guests' network,
    # so every probe reaches them through a gateway. If the gateway is down,
    # every downstream probe fails for a NETWORK reason — without this, one
    # gateway outage looks like every service breaking simultaneously.
    #
    # A failed dependency yields UNKNOWN downstream, never FAILED: we genuinely
    # could not look, which is not evidence of anything about the service.
    depends_on: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    # Extra slugs that must be UP (woken + held) before THIS service's backup,
    # on top of its policy's `requires` — e.g. its database service. Unrelated
    # to `depends_on` above, which only affects health verdicts.
    backup_requires: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    # The operator's MANUAL address for this service ("" = none). Wins over
    # anything discovered (ServiceState.discovered_ip), and is the only IP
    # that lives in services.yaml — discovery is runtime state and never makes
    # the registry dirty. Validated by registry.ip_address.validate_ip_address.
    # See docs/service_ips.md.
    ip_address: str = ""

    node_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("node.id", ondelete="RESTRICT")),
    )
    backup_policy_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("backup_policy.id", ondelete="SET NULL")),
    )

    # The service tree (docs/plan_service_tree.md). A sub-service is a Service
    # with a parent: a container on Portainer, an add-on on Home Assistant. It
    # has no guest of its own - it inherits the ROOT's guest and node (node_id is
    # kept equal to the root's) - and has its own probes, proxy hosts, controller,
    # recovery policy and backup setting. Deleting a parent deletes its children.
    parent_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("service.id", ondelete="CASCADE"), nullable=True),
    )
    # How to act on THIS service itself (start/stop/restart/status), e.g.
    # {"type": "portainer", "endpoint": 2, "container": "nginx"}. {} = nothing but
    # the root's guest (Proxmox) can act on it. See orchestrator.controllers.
    controller: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Limited automatic recovery. {} = off. See orchestrator.recovery.
    recovery: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # For a sub-service: does its root's backup wait for it? IGNORE (default) or
    # REQUIRED (healthy before the dump; started first if stopped).
    backup_gate: BackupGate = Field(default=BackupGate.IGNORE)

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Probe(SQLModel, table=True):
    """Per-service check descriptor. ``config`` is validated at app layer per ``kind``.

    ``required=False`` marks the probe as diagnostic-only: its result is recorded and
    surfaced on the dashboard but does not affect the aggregated verdict / gate.
    ``enabled=False`` goes further: the probe is not run at all.
    """

    __tablename__ = "probe"

    id: int | None = Field(default=None, primary_key=True)
    service_id: int = Field(
        sa_column=Column(ForeignKey("service.id", ondelete="CASCADE"), nullable=False),
    )
    name: str
    kind: ProbeKind
    required: bool = Field(default=True)
    timeout_s: int = Field(default=15)
    order: int = Field(default=0)
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    #: False = switched off, not deleted: the health engine records the probe as
    #: skipped ("disabled") and it never counts towards the verdict, the backup
    #: gate, the health history or the service badge. ``required`` / timeout
    #: survive a disable -> enable. The auto-managed ``traefik:<hostname>`` probe
    #: follows its proxy host's ``enabled`` flag.
    enabled: bool = Field(default=True)


class ProxyHost(SQLModel, table=True):
    """Traefik / Pangolin / NPM route observed for a service.

    Seam introduced from day 1 so phases 5 and 7 have somewhere to write. In
    phase 1 this table is created but not populated.
    """

    __tablename__ = "proxy_host"

    id: int | None = Field(default=None, primary_key=True)
    service_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("service.id", ondelete="SET NULL")),
    )
    hostname: str = Field(index=True)
    upstream: str = ""
    router_provider: RouterProvider = Field(default=RouterProvider.TRAEFIK)
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    last_seen_at: datetime | None = None

    # Per-route opt-in for the TrafficCollector-driven "wake on hit" stand-in
    # (see orchestrator.scheduler.traffic_collector and docs/traffic_metrics.md
    # for the full three-way gate with Service.wake_on_hit_enabled and
    # Node.wake_on_hit_enabled). Defaults False — nobody currently has this
    # behaviour (it did not exist before), so upgrading must never start
    # auto-waking a service that was previously left alone. An operator
    # opts a hostname in explicitly, per route.
    wake_on_hit: bool = Field(default=False)

    # Entry points the live-managed route is bound to and the certificate
    # resolver its TLS uses (empty = none). Chosen by the operator from what
    # Traefik really has - Traefik Manager would otherwise default an empty
    # selection to an `https` entry point most setups do not have. An empty
    # list on a row means "not stored yet" (created before this existed).
    entry_points: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    cert_resolver: str = ""
    # How the live route is built beyond hostname/upstream/entry points - see
    # adapters.proxy.base.RouteOptions: pass_host_header (default True),
    # skip_upstream_tls_verify, tls_main / tls_sans (a wildcard or multi-name
    # certificate request), http_redirect (companion HTTP -> HTTPS router).
    # Empty = "use the defaults" (every row that predates this).
    route_options: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # False = the route is switched off, not deleted. For a live-managed host
    # Traefik Manager parks the router (and its service) in its own
    # ``disabled_routes`` store, so Traefik stops serving it; the record and
    # every setting stay here so Enable brings back exactly the same route.
    # See docs/proxy_hosts.md "Enabling and disabling a host".
    enabled: bool = Field(default=True)
    # Fingerprint of everything the orchestrator last PUSHED to the outside
    # service for this host (hostname, upstream, entry points, cert resolver,
    # route options, enabled), recorded only after a push succeeded. Empty =
    # unknown (never pushed from here). Runtime state like the discovered IP:
    # NOT part of services.yaml, so it never makes the registry dirty. Edits
    # change the DB only; the difference between this and the host's current
    # fingerprint is what "pending push" means. See registry/proxy_push.py.
    pushed_sig: str = Field(default="", sa_column_kwargs={"server_default": ""})


# ---------------------------------------------------------------------------
# Runtime telemetry — DB-only, never touched by YAML reconcile.
# ---------------------------------------------------------------------------


class TrafficSample(SQLModel, table=True):
    """One poll's raw Traefik counter snapshot for one router, attributed to
    the :class:`ProxyHost` whose hostname that router currently serves.

    DB-only, never rehydrated from YAML — this is runtime telemetry, not
    registry config. Stores RAW cumulative counter values (not deltas): a
    reader computes a rate as ``(b.requests_total - a.requests_total) /
    (b.sampled_at - a.sampled_at)`` over whatever window it wants, which is
    robust to a missed poll (a longer window, not wrong data) and lets the
    whole trend be reconstructed from the stored rows alone. A decrease
    between consecutive samples means Traefik restarted (its counters reset
    to 0) — readers must treat that as "no delta available", never as
    negative traffic.

    Only routers that resolve to an existing ``ProxyHost`` (by hostname) are
    ever stored — see ``orchestrator.scheduler.traffic_collector`` — so
    every row can always be attributed to a ``Service`` via its proxy host.
    ``proxy_host_id`` is therefore required, unlike ``ProxyHost.service_id``.
    """

    __tablename__ = "traffic_sample"

    id: int | None = Field(default=None, primary_key=True)
    proxy_host_id: int = Field(
        sa_column=Column(ForeignKey("proxy_host.id", ondelete="CASCADE"), nullable=False),
    )
    sampled_at: datetime = Field(default_factory=_utcnow, index=True)
    requests_total: int = 0
    duration_sum_s: float = 0.0
    duration_count: int = 0
    # Cumulative per-status-code counters at this poll, e.g. {"200": 1180, "404": 12}.
    by_code: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class GeoTrafficSample(SQLModel, table=True):
    """One aggregated count of hits from one (``client_ip``, ``proxy_host``,
    interval) tuple, resolved to a country — the GeoIP companion to
    :class:`TrafficSample`, sourced from Traefik's own access log (tailed
    through Traefik Manager, see ``orchestrator.adapters.proxy.base
    .ProxyAdapter.tail_access_log``) instead of its aggregate Prometheus
    counters, since per-request client IPs only ever appear in the access
    log.

    Deliberately compact, same philosophy as :class:`HealthScanSample`: one
    row per poll cycle per distinct ``(proxy_host_id, client_ip)`` pair seen
    that cycle, with ``hit_count`` holding how many NEW access-log lines
    (since the collector's own cursor) matched it — not one row per
    request, which would grow unbounded on a busy homelab. This is a
    monitoring trend view ("which countries/IPs hit us, how often, against
    which service"), not a forensic per-request log — the raw lines
    themselves already live in Traefik's own access log file; this does not
    duplicate them. See ``orchestrator.scheduler.access_log_collector`` and
    ``docs/geoip_traffic.md``.

    Only entries whose router resolves to an existing ``ProxyHost`` are
    stored — same reasoning as ``TrafficSample``: a router with no matching
    ``ProxyHost`` has no ``Service`` to attribute the hit to, so there is
    nothing useful to keep.
    """

    __tablename__ = "geo_traffic_sample"

    id: int | None = Field(default=None, primary_key=True)
    proxy_host_id: int = Field(
        sa_column=Column(ForeignKey("proxy_host.id", ondelete="CASCADE"), nullable=False),
    )
    sampled_at: datetime = Field(default_factory=_utcnow, index=True)
    client_ip: str = Field(index=True)
    # "local" | "country" | "unknown" — see orchestrator.registry.geoip.
    bucket: str = "unknown"
    country_code: str = ""
    country_name: str = ""
    city: str = ""
    # Most recent status code observed for this tuple this cycle — enough to
    # eyeball "is this country mostly 200s or mostly 404/403 probing," not a
    # full per-code breakdown (that level of detail already exists per-host
    # on TrafficSample.by_code).
    status_code: int = 0
    hit_count: int = Field(default=1)


class TrafficRollup(SQLModel, table=True):
    """Per-minute request counters for one proxy host, rolled up from
    Traefik's access log (tailed through Traefik Manager) by
    ``orchestrator.scheduler.access_log_collector`` — what ``/traffic`` and
    ``GET /api/traffic/series`` read.

    One row per ``(proxy_host_id, bucket_start)``; ``bucket_start`` is the
    UTC minute the requests STARTED in. A minute can straddle two polls, so
    the collector upserts by ADDING into an existing row rather than
    replacing it. Durations are kept as sum/count/max so any coarser bucket
    (5 min, 1 h) can still be averaged exactly. Short retention
    (``TRAFFIC_ROLLUP_RETENTION_DAYS``), CASCADE on proxy-host delete — same
    reasoning as :class:`TrafficSample`.
    """

    __tablename__ = "traffic_rollup"
    __table_args__ = (
        UniqueConstraint("proxy_host_id", "bucket_start", name="uq_traffic_rollup_host_bucket"),
    )

    id: int | None = Field(default=None, primary_key=True)
    proxy_host_id: int = Field(
        sa_column=Column(ForeignKey("proxy_host.id", ondelete="CASCADE"), nullable=False),
    )
    bucket_start: datetime = Field(index=True)
    hits: int = 0
    s2xx: int = 0
    s3xx: int = 0
    s4xx: int = 0
    s5xx: int = 0
    duration_ms_sum: float = 0.0
    duration_ms_count: int = 0
    duration_ms_max: float | None = None


class HealthScanSample(SQLModel, table=True):
    """One periodic scan cycle's verdict for one service — the history
    :class:`ServiceState` does not keep (it only ever holds the LATEST verdict).

    Deliberately compact: just enough for a trend view (a strip of recent
    ticks on the service page), not a forensic record. The full probe-by-probe
    detail for the latest scan already lives on ``ServiceState.last_probe_results``,
    and a specific probe can be re-run on demand via the ``probe-test`` tool
    (CLI + dashboard) — duplicating that detail here, per sample, per probe,
    forever, is exactly the "takes up a lot of space" outcome this table is
    meant to avoid. See ``orchestrator.scheduler.health_scanner``.

    ``reason`` is the same short string :class:`ServiceVerdict.reason` already
    produces, truncated defensively — it is meant to read as "why", not to
    hold an essay.
    """

    __tablename__ = "health_scan_sample"

    id: int | None = Field(default=None, primary_key=True)
    service_id: int = Field(
        sa_column=Column(ForeignKey("service.id", ondelete="CASCADE"), nullable=False, index=True),
    )
    sampled_at: datetime = Field(default_factory=_utcnow, index=True)
    state: HealthState = Field(default=HealthState.UNKNOWN)
    reason: str = ""


# ---------------------------------------------------------------------------
# Admins + passkeys — DB-only. NOT rehydrated from YAML, otherwise reloading
# services.yaml would lock the operator out.
# ---------------------------------------------------------------------------


class Admin(SQLModel, table=True):
    __tablename__ = "admin"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    disabled_at: datetime | None = None


class WebAuthnCredential(SQLModel, table=True):
    __tablename__ = "webauthn_credential"

    id: int | None = Field(default=None, primary_key=True)
    admin_id: int = Field(
        sa_column=Column(ForeignKey("admin.id", ondelete="CASCADE"), nullable=False),
    )
    credential_id: bytes = Field(index=True, unique=True)
    public_key: bytes
    sign_count: int = Field(default=0)
    transports: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    aaguid: str = ""
    label: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: datetime | None = None


# ---------------------------------------------------------------------------
# Runtime state — never touched by YAML reconcile.
# ---------------------------------------------------------------------------


class ServiceState(SQLModel, table=True):
    """One row per service; rewritten by the health engine."""

    __tablename__ = "service_state"

    service_id: int = Field(
        sa_column=Column(
            ForeignKey("service.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    last_verdict: HealthState = Field(default=HealthState.UNKNOWN)
    last_verdict_at: datetime | None = None
    last_probe_results: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON),
    )
    last_backup_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("backup_record.id", ondelete="SET NULL")),
    )
    notes: str = ""

    # --- IP discovery (orchestrator.ipdiscovery) — runtime only, never YAML.
    # Last address found for the guest; kept when a later run finds nothing
    # (a stopped agent does not mean the address changed).
    discovered_ip: str = ""
    #: Who answered: "guest_agent" | "lxc" | "arp" | "dhcp" ("" = nobody yet).
    ip_source: str = ""
    #: The guest's NIC MACs as Proxmox reports them (lowercase, colon form).
    mac_addresses: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    ip_discovered_at: datetime | None = None
    #: Why the last run found no address ("" when it did).
    ip_discovery_error: str = ""


class NodeState(SQLModel, table=True):
    """One row per node. ``hold_count`` prevents the usage-based shutdown from
    firing while a wake or backup is in progress; ``current_hold_reasons`` records
    who is holding it and why so a stuck hold can be diagnosed."""

    __tablename__ = "node_state"

    node_id: int = Field(
        sa_column=Column(
            ForeignKey("node.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    power_state: PowerState = Field(default=PowerState.UNKNOWN)
    power_state_at: datetime | None = None
    last_seen_at: datetime | None = None
    hold_count: int = Field(default=0)
    current_hold_reasons: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON),
    )


class PipelineRun(SQLModel, table=True):
    """Every wake / backup / restore pipeline run. Used for the dashboard timeline,
    resume-after-restart logic, and audit joins."""

    __tablename__ = "pipeline_run"

    id: int | None = Field(default=None, primary_key=True)
    kind: PipelineKind
    correlation_id: str = Field(index=True)
    service_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("service.id", ondelete="SET NULL")),
    )
    status: PipelineStatus = Field(default=PipelineStatus.QUEUED, index=True)
    started_at: datetime = Field(default_factory=_utcnow, index=True)
    finished_at: datetime | None = None
    error: str | None = None
    # steps: list of {"name": str, "started_at": iso, "finished_at": iso, "status": str, "detail": {...}}
    steps: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))


class BackupRecord(SQLModel, table=True):
    __tablename__ = "backup_record"

    id: int | None = Field(default=None, primary_key=True)
    service_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("service.id", ondelete="SET NULL")),
    )
    # Denormalised so the record still identifies its origin after a service is
    # removed from YAML and cascaded out of ``service``.
    service_slug: str = Field(index=True)
    policy_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("backup_policy.id", ondelete="SET NULL")),
    )
    pipeline_run_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("pipeline_run.id", ondelete="SET NULL")),
    )
    pbs_snapshot_id: str = Field(index=True)  # e.g. "vm/9001/2026-08-24T02:00:00Z"
    size_bytes: int | None = None
    verified: bool = Field(default=False)
    verified_at: datetime | None = None
    protected: bool = Field(default=False, index=True)  # the known-good pin
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class AuditEntry(SQLModel, table=True):
    """Append-only. Insertions only — never updated or deleted from application code."""

    __tablename__ = "audit_entry"

    id: int | None = Field(default=None, primary_key=True)
    actor: str = Field(index=True)  # username or "system"
    action: str = Field(index=True)  # e.g. "wake.request", "backup.gate.failed"
    target: str = ""  # free-form: "service:example-media"
    correlation_id: str | None = Field(default=None, index=True)
    result: AuditResult = Field(default=AuditResult.OK)
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class PendingApproval(SQLModel, table=True):
    """Two-phase confirmation token for high-risk actions.

    The token is what an approve-link carries; ``payload`` is the *exact* action
    that will be executed — never re-derived at approval time."""

    __tablename__ = "pending_approval"

    id: int | None = Field(default=None, primary_key=True)
    kind: ApprovalKind
    token: str = Field(index=True, unique=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    requested_by: str = ""
    requested_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, index=True)


class StateSnapshotMarker(SQLModel, table=True):
    """Bookkeeping for the periodic SQLite → NFS dump job."""

    __tablename__ = "state_snapshot_marker"

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # "pre_migration", "scheduled", "manual"
    path: str
    size_bytes: int | None = None
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class NotificationRule(SQLModel, table=True):
    """Who hears about what. An empty list means "any" for that dimension.

    With NO rules at all the engine falls back to "failed / down / recovered at
    every level, on every channel", so notifications work as soon as a channel is
    configured. Plain strings, not Enums, on purpose (the stored value is exactly
    what the UI sends)."""

    __tablename__ = "notification_rule"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    enabled: bool = Field(default=True)
    #: Subset of node | guest | service | sub.
    levels: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    #: {"slugs": [...], "groups": [...], "nodes": [...]} - any match counts.
    scope: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    #: failed | down | recovered | flapping | recovery_attempt | recovery_exhausted | ...
    events: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    #: info | warn | error | critical - the least severe event this rule wants.
    min_severity: str = "info"
    #: Channel names (ntfy, email, webhook); empty = every configured channel.
    channels: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class NotificationEvent(SQLModel, table=True):
    """Every event the engine decided on, sent or not - the feed on /notifications."""

    __tablename__ = "notification_event"

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=_utcnow, index=True)
    level: str = Field(index=True)  # node | guest | service | sub
    target: str = Field(index=True)  # slug, node name, or "node/vmid" for a guest
    name: str = ""
    event: str = Field(index=True)
    severity: str = "info"
    title: str = ""
    body: str = ""
    #: The incident this is folded into ("node:pve1"), if any.
    cause: str = ""
    suppressed: bool = False
    suppressed_reason: str = ""
    rule_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    #: {channel: "ok" | "error: ..."}
    delivered: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class TargetState(SQLModel, table=True):
    """What the notification engine last concluded about one target, with the
    debounce and flap bookkeeping. Runtime only; never in services.yaml."""

    __tablename__ = "target_state"

    level: str = Field(sa_column=Column(String, primary_key=True))
    key: str = Field(sa_column=Column(String, primary_key=True))
    state: str = "unknown"  # healthy | failed | down | unknown
    #: The bad state we actually TOLD someone about ("" = nothing outstanding), so a
    #: recovery is announced only for something that was announced.
    announced: str = ""
    since: datetime = Field(default_factory=_utcnow)
    pending_state: str = ""
    pending_count: int = 0
    transitions: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    muted_until: datetime | None = None
    updated_at: datetime = Field(default_factory=_utcnow)


class Setting(SQLModel, table=True):
    """Tiny key/value store for singleton metadata (last-loaded YAML hash, etc.).

    Used by the registry loader to detect drift between the DB (live) and the
    on-disk YAML (saved), so the dashboard can show an unsaved-changes badge."""

    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=_utcnow)


# Well-known Setting keys, so callers don't stringly-type them.
SETTING_LAST_YAML_LOAD = "registry.last_yaml_load"  # {"path", "sha256", "at"}
SETTING_LAST_YAML_SAVE = "registry.last_yaml_save"  # {"path", "sha256", "at", "by"}
#: Proxy hosts deleted here whose Traefik Manager route has not been removed
#: yet: {"items": [{"hostname", "route_ref", "with_redirect", "service_slug"}]}.
#: Cleared by anything that replaces the DB from YAML (Reset, boot reconcile).
SETTING_PROXY_PENDING_DELETES = "proxy.pending_deletes"
