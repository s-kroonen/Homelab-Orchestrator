# Architecture

## Three planes

* **Control** — this service, on the always-on Pi. Decides *what* should
  happen; never runs the checks itself.
* **Execution** — per-service checks that run **inside each guest** over SSH
  or a local API. The Pi never mounts service databases directly.
* **Data** — Proxmox nodes → PBS. The orchestrator's SQLite is state *about*
  the data plane, not part of it.

## In-path vs out-of-path

Traefik + Pangolin are in-path on the always-on gateway. The orchestrator is
touched in the request flow **only** when a backend is down — Traefik's
`errors` middleware sends the request to the orchestrator's maintenance
responder, which renders a page and fires the wake pipeline.

Fail-open rule: if the orchestrator is down, Traefik serves a static fallback
page.  If the maintenance responder is up but the DB or scheduler are
degraded, it still renders a generic "starting…" page and still fires the
wake — it must never return a 5xx.

## Modules (phase 1 seams)

```
adapters/           external systems, all mockable + dry-run
  power/{base,mqtt,dry_run}      # existing power manager, MQTT preferred
  proxmox/{base,api,dry_run,net} # PVE API — guests, vzdump, task status, guest NICs/IPs
  pbs/{base,api,dry_run}         # PBS API — datastore, snapshots, verify, prune
  proxy/{base,traefik_manager,dry_run}  # reverse proxy — Traefik Manager today
  notifier/{base,null}           # mailcow + ntfy (phase 9)
db/                 SQLModel schema + engine
domain/             enums + Pydantic DTOs shared across modules
health/             probe engine (phase 4)
ipdiscovery/        service IP discovery: guest agent / LXC, then ARP, then
                    DHCP leases by NIC MAC (see "Service IPs" below)
pipelines/          wake (phase 3), backup (phase 2/6), restore (phase 8)
registry/           YAML <-> DB reconcile + save/reset; also the direct-
                    Traefik-API integration modules (status/probes) — the
                    Traefik Manager client/sync logic lives in
                    adapters/proxy/traefik_manager.py instead (see below)
scheduler/          APScheduler wiring: backups (phase 6), Traefik traffic
                    polling, periodic health scanning (HealthScanner),
                    periodic IP discovery (IpDiscoveryScheduler)
web/                FastAPI app: health, maintenance, wake, dashboard
audit/              append-only audit writes
```

## Proxmox connectivity

The orchestrator uses a single `PROXMOX_HOST`, which must be the always-on node.
Multi-endpoint failover is a planned later phase, and there is a **quorum**
consideration that affects whether the wake pipeline can start guests at all on
a mostly-powered-off cluster. Both are covered in
[proxmox_connectivity.md](proxmox_connectivity.md).

## Health model

A scan runs three stages in order; each one short-circuits the rest:

1. **Guest gate** (`health/guest_gate.py`) — for a service with a guest
   (`guest_kind` vm/ct and `guest_id` set): is its node online in the Proxmox
   cluster (`cluster_status`) and is the guest `running` (`list_guests`)? If
   not, the verdict is **DOWN** and no probe runs. The check is recorded as the
   first entry in the verdict's probe results (name `guest`, kind `guest`) and
   every probe is recorded as `skipped — guest stopped` / `skipped — node
   offline`. If Proxmox cannot be read, the gate steps aside: the probes run as
   before and the reason notes "guest state unknown".
2. **Dependencies** — any `depends_on` service not HEALTHY makes this one
   UNKNOWN (`blocked: dependency 'nas' is DOWN (guest vm 9001 is stopped)…`).
3. **Probes** — the required probes aggregate into one of the states below.
   A probe with `enabled: false` is not run: it is recorded as `skipped —
   disabled` and never counted (not in the aggregation, the backup gate, the
   health history or the badge); with every probe disabled the "no required
   probe -> UNKNOWN" rule applies. The auto-created `traefik:<hostname>` probe
   follows its proxy host's `enabled` flag (kept with its `required` / timeout
   while off) - see [proxy_hosts.md](proxy_hosts.md#feeding-traefiks-view-into-the-services-own-health-verdict).

| State    | Meaning                                                                                          | Backup gate |
| -------- | ------------------------------------------------------------------------------------------------ | ----------- |
| HEALTHY  | Node online, guest running AND all required probes pass (containers healthy, DB integrity ok).   | Runs        |
| FAILED   | A definitive negative: unit failed, container exited/unhealthy, HTTP 5xx, integrity check fails. | Skipped, restore candidate. |
| UNKNOWN  | Indeterminate — probe timed out, node unreachable, DB check couldn't run, dependency blocked.    | Skipped, alert only. |
| DOWN     | The guest gate: node offline or guest not running. Probes were not run.                          | Skipped; **not** a restore candidate. |

**Never overwrite a good backup with a bad one.** UNKNOWN is treated as
non-clean (fail closed) so a network blip cannot cascade a failed backup.

**DOWN is not FAILED.** A stopped VM is not evidence of corrupt data and must
never look like a restore candidate. It is not UNKNOWN either: we know exactly
why the service is not answering, and so is its traffic data (on `/traffic` a
DOWN service's hosts are flagged "guest stopped — requests to it are
failing"). Wake-on-hit treats DOWN like FAILED — it is the clearest sign a wake
is what's needed.

The guest state is fetched once per scan cycle and shared by every service in
it (the HealthScanner hands one `GuestStateCache` to all of them, so N services
cost two Proxmox calls). A one-off scan — the backup gate, the wake pipeline's
health poll, `orchestrator-cli scan` — reads it fresh. `HealthState` is
persisted by name into plain string columns (`service_state.last_verdict`,
`health_scan_sample.state`), so DOWN needed no migration. A `HealthEngine`
constructed without a `proxmox=` adapter has no guest gate; every real
construction site (scanner, wake and backup pipelines, CLI `scan`) passes one.

### Periodic scanning and the shared-cache consistency guarantee

`HealthEngine.scan` persists its verdict to `service_state` (one row per
service — only the LATEST verdict, no history) as a side effect every time
it is called. Before `HealthScanner` existed, the only callers were the
CLI's manual `scan` command, `WakePipeline`, and `BackupPipeline`'s gate
check — none on any schedule, and each starting its own fresh dependency
cache. That had a real consequence beyond staleness: if service A and
service B both `depends_on: [gateway]` but were scanned at different real
moments (A during its own backup's gate check, B a minute later during a
wake), each top-level call re-probed "gateway" LIVE at its own moment, and a
flaky/slow network path could answer differently each time — so A, B and the
gateway's own row could end up disagreeing with each other about whether the
gateway was even healthy, even though nothing was actually wrong with the
aggregation rule.

`orchestrator.scheduler.health_scanner.HealthScanner` runs one scan cycle
every `HEALTH_SCAN_INTERVAL_S` (default 600s / 10 min, `HEALTH_SCAN_ENABLED`
to turn it off). Within one cycle it scans every enabled service, passing
the **same** `_cache: dict[str, ServiceVerdict]` into every one of those
top-level `engine.scan(...)` calls — the identical cache
`HealthEngine._check_dependencies` already uses internally to avoid
re-probing a dependency already scanned earlier in the same call tree, now
shared across the whole cycle's top-level services too. A gateway behind N
services is therefore probed **exactly once per cycle**, and every
dependent — plus the gateway's own row — agrees by construction, because
they were all judged off the exact same verdict for that cycle. This does
not make verdicts instantaneous (one can still be up to one cycle old), only
internally consistent within a cycle, which is what eliminates the
gateway-vs-dependent mismatch an operator could otherwise observe.

Each cycle also writes one `health_scan_sample` row per slug that ended up
in that cycle's cache — which naturally includes every enabled top-level
service *and* any dependency pulled in along the way, even a disabled
service that something still `depends_on` (the dependency chain does not
care about `enabled`, so leaving it out of the history would be
inconsistent with what was actually probed). This is the trend history
`service_state` does not keep: a compact per-service strip ("was this
healthy an hour ago?"), deliberately narrow-columned (`sampled_at`, `state`,
a truncated `reason`) and short-retention (`HEALTH_HISTORY_RETENTION_DAYS`,
default 7 days, pruned every cycle) — this is a monitoring trend view, not
an audit trail; `audit_entry`/`pipeline_run` already serve that purpose for
backups and wakes, and the full per-probe detail for the *current* verdict
already lives on `service_state.last_probe_results` (re-run on demand via
the `probe-test` tool).

The dashboard and service pages poll themselves every 60s
(`setInterval(() => location.reload(), 60000)`, same pattern as the
existing `/traffic` auto-refresh) so an open tab shows fresh health without
a manual reload — independent of, and much shorter than, the scanner's own
cycle, since re-rendering state already in the DB is cheap. The service
page's refresh is guarded to skip while the admin is mid-edit (focused in a
form field, or the probe-add panel is open), so the periodic refresh never
discards in-progress input.

Manual, on-demand scanning (the CLI's `scan`/`probe-test`) still exists
alongside the periodic path and is unaffected by it — see
[adding_a_service.md](adding_a_service.md). The two serve different
purposes: manual is "debug this one thing right now" (its own fresh cache,
one service at a time), periodic is "keep the whole fleet's state fresh and
mutually consistent in the background."

## Wake-unsupported: structurally impossible vs merely failed

Every failure the wake pipeline (`orchestrator.pipelines.wake`) could hit
used to look the same to a visitor on the maintenance page: a generic
"Could not start" with a retry link. That conflates two very different
situations:

* **Failed or timed out** — the attempt itself went wrong (Proxmox
  unreachable, a non-quorate cluster, the guest didn't become healthy in
  time). Retrying is at least plausible; the operator or visitor might
  reasonably try again later.
* **Structurally unsupported** — this service's node has `power_mgr_target`
  blank AND is not marked `always_on`. There is no command the orchestrator
  could ever send to turn it on. Retrying can never help, no matter how
  long you wait.

`orchestrator.pipelines.wake.wake_is_unsupported(node)` is the one place
this condition is defined (`node is None`, or both `power_mgr_target` is
blank and `always_on` is false), shared by three call sites so they can
never drift apart:

1. `WakePipeline._resolve` raises `WakeUnsupportedError` (a `WakeError`
   subclass — every existing `except WakeError` still catches it) as early
   as possible, before any hold is asserted or power command sent.
2. `web/routers/maintenance.py` recomputes the same condition directly from
   the service's node, **before** even calling into the wake pipeline, and
   renders a visually distinct page: a different `spinner_class`
   (`"unsupported"`, styled amber in `maintenance.html`, not spinning, not
   red) and `show_retry=False`. This recompute-rather-than-inspect-the-
   pipeline-result approach matters beyond the very first visitor: a second
   visitor's page load reuses a prior `PipelineRun` via `get_or_start`'s
   cooldown/RUNNING-reuse logic (see `docs/proxy_hosts.md`-adjacent wake
   docs) without re-raising, so relying solely on catching the exception
   from the pipeline would only show the distinct page on the very first
   hit.
3. `web/routers/dashboard.py`'s `service_detail()` includes a
   `wake_unsupported` boolean in the same payload the service detail page
   already renders, so an admin sees which services this applies to up
   front — a banner on `/services/<slug>` — rather than only discovering it
   when a visitor actually hits a down backend.

**Deliberately excluded**: `PowerAdapter.IMPLEMENTED is False` (an
unimplemented/dry-run power adapter) does NOT count as wake-unsupported.
That is a different kind of "not ready" — a deliberate, operator-visible
deployment-time choice (`DRY_RUN=true`, or a power adapter not yet wired
up) the operator already knows about from their own `.env`, not a per-node
data gap a visitor might be the first to discover. Treating it the same way
would also make every service in a dry-run/staging deployment show this
page, which is the expected normal state there, not something worth an
administrator-contact message.

**Deliberately NOT checked**: whether the node happens to already be
online right now. That would need an async Proxmox call, and this check is
used from synchronous contexts (`WakePipeline.start`, the maintenance
page's early check) specifically so it fires before anything else is
attempted. The cost of this simplification is a node that's already on but
happens to have no `power_mgr_target` set also shows this page — a rare
edge case, and arguably still correct: the data gap is real even if it
hasn't bitten anyone yet.

## Waking for a backup

`BackupPipeline.run_for_service` begins with a wake phase
(`orchestrator.pipelines.backup_wake`), **before** the PBS preflight, because the
datastore may sit on NFS served by a VM on a normally-off node. What to wake comes
from one pure function, `registry/backup_plan.resolve_backup_plan`
(`BackupPolicy.requires`, then `Service.backup_requires`, then the target when
`BackupPolicy.wake_before_backup`), which the pipeline executes and
`GET /api/backup/plan`, `orchestrator-cli backup plan` and the service page
preview — so a preview cannot drift from a run.

The waking itself is not reimplemented: each entry goes through
`WakePipeline.ensure_awake`, which uses `get_or_start` (so a backup joins a wake a
visitor already started, and the retry cooldown applies). What is new in
`wake.py` is the public `assert_hold` / `release_hold` (the backup holds each node
for the whole backup, not just the wake) and `is_up`, a cheap "needs waking at all?"
check. `StepRecorder` and `BackupError` moved to `pipelines/steps.py` so the two
pipelines can import each other's pieces without a cycle.

Holds are released in a `finally`; a failed wake fails the run at that step with the
gate and dump never reached. A scheduled policy wakes its policy-level prerequisites
once for the batch (`BackupPipeline.prepare_policy`) and passes that context to each
per-service run. `requires` / `backup_requires` are **not** `Service.depends_on`
(health-gate dependency, never wakes). See [backup_wake.md](backup_wake.md).

## Manual power actions: two raw actions, deliberately not the wake pipeline

Two endpoints exist purely for an admin who wants a specific low-level
action right now, bypassing the orchestration `POST /wake/{slug}` does
(node power-on, a hold, guest start, health wait). Neither is used by the
wake pipeline, the backup pipeline, or the maintenance page — they exist
only for the dashboard buttons that call them.

**`POST /api/infra/nodes/{name}/power`** (`web/routers/infra.py`) —
`{"action": "wake" | "power_off" | "restart", "force": bool}` — calls
`adapters.power.wake`/`power_off`/`restart` directly for the named `Node`
(`node.power_mgr_target`, not the Proxmox node name itself — same
distinction `WakePipeline` makes). `force` (default `false`) maps straight
through to `PowerAdapter`'s own semantics — see
`orchestrator/adapters/power/base.py`'s docstrings: `false` asks for a
graceful shutdown/reset, `true` cuts power immediately (power_off) or does
a full power-cycle (restart), with no chance for a clean shutdown. Only
meaningful for `power_off`/`restart`; ignored for `wake`. Audit-logged as
`node.power_action`. The dashboard's Nodes table (`dashboard.html`) gets
Wake/Power off/Restart buttons plus a shared "force" checkbox; `power_off`/
`restart` are gated behind a `confirm()` that spells out exactly what
`force=true` means, `wake` is unconditional (harmless).

**`POST /api/services/{slug}/guest/power`** (`web/routers/dashboard.py`) —
`{"action": "start" | "stop"}` — calls `adapters.proxmox.start_guest`/
`stop_guest` directly for the service's guest (`node`/`vmid`/`kind`
resolved from the `Service`/`Node` rows the same way
`WakePipeline._ensure_guest_running` does), with no node power-on, no hold,
and no health wait layered on top — genuinely just "start/stop this
guest". `stop` is refused (`409`) while a backup `PipelineRun` is currently
`RUNNING` for that service (queried directly — `kind == BACKUP`,
`status == RUNNING`, `service_id` match; no existing helper covers this,
since `BackupPipeline.run_for_service` runs synchronously within one
request, so this guard normally only matters for a genuinely concurrent
request), so an admin can't yank the guest out from under a live `vzdump`.
`start` has no such guard. Audit-logged as `service.guest_power_action`.
`service_detail.html` gets Start/Stop guest buttons near the service's
node/guest fields, hidden when the service has no guest configured; `stop`
is gated behind a `confirm()`, `start` is not.

## Infra inspection pages: dashboard equivalents of the CLI's read-only commands

`web/routers/infra.py`'s other three endpoints — `GET /api/infra/status`,
`GET /api/infra/proxmox/guests`, `GET /api/infra/pbs/snapshots` — predate the
manual power actions above and map directly to `orchestrator-cli`'s
`check`/`guests`/`snapshots` commands, but had no dashboard page of their
own until the left-sidebar nav added one each: `/infra` (Connectivity),
`/infra/guests`, `/infra/snapshots` (`web/routers/ui.py`,
`infra_status.html`/`infra_guests.html`/`infra_snapshots.html`). Same
pattern as every other list-only page: the server renders the shell only,
the table is filled client-side by `apiFetch` against the existing
endpoints — no new API surface, no mutation.

## Deferred push (DB = working copy, outside services written on Save / Push)

Dashboard edits only change the DB, as they always did for the registry; what
is new is that this now holds for outside services too. `orchestrator.registry.
push` defines a small `Pusher` protocol (`name`, `status(session, adapters)`,
`apply(session, adapters, only=None, *, dry_run, actor)`) and a registry
(`register_pusher`, `status_all`, `apply_all`). `ProxyPusher`
(`registry/proxy_push.py`) is the only one today: it classifies every proxy
host (`in_sync`, `pending_create`, `pending_update`, `detached`, `external`,
`local`) by comparing the DB row, the fingerprint of what was last pushed
(`ProxyHost.pushed_sig`) and what Traefik Manager lists, and applies the
difference in a safe order (create, enable, update, maintenance fallback,
disable last, verify). Pending deletions are kept in the `Setting` row
`proxy.pending_deletes`. `POST /api/registry/save` writes the YAML first and
then runs every registered pusher; `GET /api/push/status`, `POST /api/push` and
`orchestrator-cli proxy status|push` use the same engine. Backup or power
adapters that later want "edit locally, push on save" register their own
pusher. Details: `docs/proxy_hosts.md` "Pushing to Traefik Manager".

## Traefik Manager (write path)

Until this phase, every Traefik integration in this app was read-only
(direct polls of Traefik's own API — see `docs/proxy_hosts.md`'s drift
detection and the old Resync path). Traefik Manager
(https://github.com/chr0nzz/traefik-manager) is now installed on the
gateway alongside Traefik and is the stated source of truth for proxy
routes; the orchestrator talks to it through a `ProxyAdapter`
(`src/orchestrator/adapters/proxy/`), the same `dry_run`/live-implementation
shape `adapters.{power,proxmox,pbs}` already use. Its methods:
`list_routes` (read path), `ensure_maintenance_fallback`/
`remove_maintenance_fallback` (the maintenance-page fallback middleware,
fail-open), and `create_route`/`update_route`/`delete_route`/
`set_route_enabled` (general route CRUD backing live-managed proxy hosts —
raises on failure; a toggle is read back, never trusted on a 200). **These
writes no longer happen on an edit.** The database is the working copy and the
adapter is only called by the push engine (below). Traefik Manager's
`file::name` route ids (several config files) are resolved by the adapter. See
`src/orchestrator/adapters/proxy/traefik_manager.py`
(`TraefikManagerProxyAdapter` — the REST calls and the maintenance-middleware
feature built on top of them) and `docs/proxy_hosts.md` for the full
design. This used to be two standalone modules under `orchestrator.registry`
kept deliberately outside `orchestrator.adapters`; see `docs/roadmap.md`'s
"generalize the adapter pattern" entry for why that changed.

New settings:

| Setting | Purpose | Default |
|---|---|---|
| `PROXY_ADAPTER` | `traefik_manager` \| `dry_run`. A live value with `TRAEFIK_MANAGER_URL` blank falls back to `dry_run`, loudly (same "substitute + warn" convention `MqttPowerAdapter.IMPLEMENTED` uses) — see `adapters/factory.py::build_adapters`. | `dry_run` |
| `TRAEFIK_MANAGER_URL` | Base URL. Empty disables the read-path swap AND the middleware-injection feature AND forces `PROXY_ADAPTER` to `dry_run` regardless of its own setting. | `""` |
| `TRAEFIK_MANAGER_API_KEY` | `X-Api-Key` header, generated per-device in Traefik Manager's own UI. | `""` |
| `TRAEFIK_DEFAULT_ENTRYPOINTS` | Comma list pre-selected for a NEW live-managed proxy host. Blank = `websecure` if it exists on the live Traefik, else the first entry point that is not `traefik`, else the operator must choose. Only a pre-selection — see `docs/proxy_hosts.md#entry-points-and-certificate-resolver`. | `""` |
| `TRAEFIK_DEFAULT_CERT_RESOLVER` | Certificate resolver pre-selected for a new host on a TLS entry point. Blank = none. | `""` |
| `TRAEFIK_MANAGER_VERIFY_TLS` | TLS verification for that connection. | `true` |
| `TRAEFIK_MANAGER_TIMEOUT_S` | Short — runs synchronously from a dashboard-triggered request. | `5.0` |
| `ORCHESTRATOR_PUBLIC_URL` | This orchestrator's own address as reachable FROM THE GATEWAY VM — the backend target for the maintenance-page fallback service. Unlike `TRAEFIK_MANAGER_URL`, does NOT affect which adapter is selected — it only gates the middleware-injection feature itself (the read-path swap only needs `TRAEFIK_MANAGER_URL`). | `""` |

No new DB tables or columns were needed for this — the middleware
injection's state of record lives entirely in Traefik's own config files
(managed by Traefik Manager), not in this app's database; the orchestrator
only ever computes what should be there and syncs it on `ProxyHost`
mutation, the same way `traefik_probes.sync_traefik_probe` already works
for the auto-managed diagnostic probe. Exactly one new audit-action family
(`backend_service.created`, `middleware.created`/`updated`/`injected`/
`removed`) was added, following the existing `proxy_host.*`/`probe.*`
naming convention.

## GeoIP traffic visibility (opt-in)

Per-request client-IP/country visibility, sourced from Traefik's **access
log** (not its aggregate Prometheus counters `traffic_metrics.md` already
covers — those have no per-request client IPs) — tailed through Traefik
Manager's existing `GET /api/traefik/logs?lines=N`
(`ProxyAdapter.tail_access_log`), never a new direct SSH/filesystem
integration. Gated entirely by `GEOIP_ENABLED` (default `false`), and
independent of `TRAEFIK_API_URL`/`TRAEFIK_METRICS_*` — it is a different
feature with a different on/off switch and failure mode, same reasoning
`TrafficCollector` and `HealthScanner` stay separate schedulers from each
other. See `docs/geoip_traffic.md` for the full design (dedup/cursor
strategy, the local-IP bucket, the GeoIP database). **The dashboard page
for this is a deferred follow-up** — see `docs/roadmap.md`.

**Update — one access-log pipeline.** The GeoIP collector has since become
`orchestrator.scheduler.access_log_collector.AccessLogCollector`, which tails
the access log once per poll and feeds BOTH `traffic_rollup` (per-minute
counters behind `/traffic`) and `geo_traffic_sample` (only when
`GEOIP_ENABLED`). It runs whenever the proxy adapter is a live Traefik
Manager (`ACCESS_LOG_ENABLED`, default `true`) and attributes lines to proxy
hosts without `TRAEFIK_API_URL` (request host, else router names from
Traefik Manager's `/api/traefik/routers` + `/api/routes`). Its last-poll
status is persisted in the `access_log.status` setting and shown on both
traffic pages. Settings: `ACCESS_LOG_ENABLED`, `ACCESS_LOG_POLL_INTERVAL_S`
(30), `ACCESS_LOG_LINES` (1000), `TRAFFIC_ROLLUP_RETENTION_DAYS` (14);
`GEOIP_TRAFFIC_POLL_INTERVAL_S` / `GEOIP_ACCESS_LOG_LINES` remain as
deprecated aliases. See `docs/traffic_metrics.md`.

| Setting | Purpose | Default |
|---|---|---|
| `GEOIP_ENABLED` | Gates the GeoIP half (samples, database, endpoint). | `false` |
| `GEOIP_DB_PATH` | A ready-made `.mmdb` file. Empty = auto-download/cache the free DB-IP city-lite database under `{STATE_DIR}/geoip/`. | `""` |
| `GEOIP_TRAFFIC_RETENTION_DAYS` | How long `GeoTrafficSample` rows are kept. | `14` |

Modules: `orchestrator.registry.access_log` (the common/JSON access-log line
parser), `orchestrator.registry.geoip` (the IP -> country lookup, wrapping
`geoip2`), `orchestrator.scheduler.access_log_collector` (the periodic
collector + dedup cursor), read endpoints `GET /api/traffic/geo`
(`web/routers/geo_traffic.py`) and `GET /api/traffic/series` /
`GET /api/traffic/collector-status` (`web/routers/traffic.py`).

## Service IPs

A service's **effective IP** is its manual `service.ip_address` (YAML-owned)
when set, else the address IP discovery last found (`service_state.
discovered_ip`, runtime). Discovery (`ipdiscovery/discovery.py`) asks, in
order, and records the first answer and its source:

1. Proxmox — `GET /nodes/{node}/{qemu|lxc}/{vmid}/config` for the NIC MACs
   (and an LXC's static `ip=`), then the QEMU guest agent's
   `network-get-interfaces` or the container's `/interfaces`;
2. the ARP table on the host `IP_DISCOVERY_TRANSPORT` names (optionally after
   an "ARP ping" sweep), looked up by MAC;
3. a dnsmasq leases file on that host, by MAC (a pluggable `DhcpLeaseSource`).

Only guests the guest-gate data says are RUNNING are asked; ARP/DHCP tables
are read once per cycle (`DiscoveryCycle`). ARP/DHCP reads go through the same
command transports probes use (host runner / ssh / local). An effective-IP
change is audited as `service.ip_changed`. Consumers: the proxy-host form's
"Use service IP" and `kind: ping` probes with a blank host (resolved when the
probe runs). Full detail: [service_ips.md](service_ips.md).

## YAML-owned vs DB-owned tables

| Table                | Ownership | Reconciled from YAML on boot? |
| -------------------- | --------- | ----------------------------- |
| `node`               | YAML      | Yes                           |
| `backup_policy`      | YAML      | Yes                           |
| `service`            | YAML      | Yes                           |
| `probe`              | YAML      | Yes                           |
| `proxy_host`         | YAML      | Yes                           |
| `admin`              | **DB**    | No — reloading YAML must not lock the operator out |
| `webauthn_credential`| **DB**    | No — same reason              |
| `service_state`      | **DB**    | No (runtime — incl. the discovered IP, its source and the NIC MACs; only `service.ip_address`, the manual IP, is YAML) |
| `node_state`         | **DB**    | No (runtime)                  |
| `pipeline_run`       | **DB**    | No (history)                  |
| `backup_record`      | **DB**    | No (history)                  |
| `audit_entry`        | **DB**    | No (append-only)              |
| `pending_approval`   | **DB**    | No                            |
| `state_snapshot_marker` | **DB** | No                            |
| `setting`            | **DB**    | No (uses two well-known keys for YAML load/save markers) |
| `traffic_sample`     | **DB**    | No (telemetry — polled from Traefik, see docs/traffic_metrics.md) |
| `health_scan_sample` | **DB**    | No (telemetry — periodic health-scan history, see "Periodic scanning" above) |
| `geo_traffic_sample` | **DB**    | No (telemetry — GeoIP traffic visibility, see "GeoIP traffic visibility" above / docs/geoip_traffic.md) |
| `traffic_rollup`     | **DB**    | No (telemetry — per-minute access-log rollups behind /traffic, see docs/traffic_metrics.md) |

## FK cascade policies

Chosen so YAML edits stay safe and history stays intact:

* `service.node_id` → `RESTRICT` (removing a node while services reference it is a YAML error, not a silent orphan).
* `probe.service_id` → `CASCADE` (probes are meaningless without their service). `probe.enabled` (migration `0016`, server default true) switches a probe off without deleting it.
* `service_state.service_id` → `CASCADE` (state is per-service).
* `proxy_host.service_id` → `SET NULL` (a route observed for a removed service is still worth keeping until scan re-verifies it's gone).
* `backup_record.service_id` → `SET NULL` + denormalised `service_slug` (backup history survives the service being removed from YAML).
* `pipeline_run.service_id` → `SET NULL` (same reasoning).
* `traffic_sample.proxy_host_id` → `CASCADE` (traffic history for a removed proxy host is not worth keeping as an orphan — unlike `backup_record`/`pipeline_run`, there is no slug to denormalise against).
* `health_scan_sample.service_id` → `CASCADE` (same reasoning — a removed service's health-scan history is not worth keeping as an orphan).
* `traffic_rollup.proxy_host_id` → `CASCADE` (same reasoning as `traffic_sample.proxy_host_id`).
* `geo_traffic_sample.proxy_host_id` → `CASCADE` (same reasoning as `traffic_sample.proxy_host_id` — GeoIP traffic history for a removed proxy host is not worth keeping as an orphan).

## Boot sequence

1. `configure_logging()`
2. Build DB engine (SQLite WAL, `PRAGMA foreign_keys=ON`, 5s busy timeout).
3. If `RUN_MIGRATIONS_ON_START=true`, `alembic upgrade head`.
4. If `services.yaml` exists, run `reconcile_yaml_into_db()`.
5. Build + start the adapter bundle.
6. Start the schedulers (backups, traffic, health scan, access log, IP
   discovery) — each non-fatal on failure.

The maintenance responder starts *before* step 4 completes so it can serve a
fallback page even if the reconcile fails.
