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
  proxmox/{base,api,dry_run}     # PVE API — guests, vzdump, task status
  pbs/{base,api,dry_run}         # PBS API — datastore, snapshots, verify, prune
  proxy/{base,traefik_manager,dry_run}  # reverse proxy — Traefik Manager today
  notifier/{base,null}           # mailcow + ntfy (phase 9)
db/                 SQLModel schema + engine
domain/             enums + Pydantic DTOs shared across modules
health/             probe engine (phase 4)
pipelines/          wake (phase 3), backup (phase 2/6), restore (phase 8)
registry/           YAML <-> DB reconcile + save/reset; also the direct-
                    Traefik-API integration modules (status/probes) — the
                    Traefik Manager client/sync logic lives in
                    adapters/proxy/traefik_manager.py instead (see below)
scheduler/          APScheduler wiring: backups (phase 6), Traefik traffic
                    polling, periodic health scanning (HealthScanner)
web/                FastAPI app: health, maintenance, wake, dashboard
audit/              append-only audit writes
```

## Proxmox connectivity

The orchestrator uses a single `PROXMOX_HOST`, which must be the always-on node.
Multi-endpoint failover is a planned later phase, and there is a **quorum**
consideration that affects whether the wake pipeline can start guests at all on
a mostly-powered-off cluster. Both are covered in
[proxmox_connectivity.md](proxmox_connectivity.md).

## Three-state health model

Every scan aggregates its required probes into one of:

| State    | Meaning                                                                                          | Backup gate |
| -------- | ------------------------------------------------------------------------------------------------ | ----------- |
| HEALTHY  | Proxmox boot ok AND all required probes pass (containers healthy, DB integrity check ok).        | Runs        |
| FAILED   | A definitive negative: unit failed, container exited/unhealthy, HTTP 5xx, integrity check fails. | Skipped, restore candidate. |
| UNKNOWN  | Indeterminate — probe timed out, node unreachable, DB check couldn't run.                        | Skipped, alert only. |

**Never overwrite a good backup with a bad one.** UNKNOWN is treated as
non-clean (fail closed) so a network blip cannot cascade a failed backup.

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
fail-open), and `create_route`/`update_route`/`delete_route` (general
route CRUD backing live-managed proxy hosts — raises on failure, since
that's the explicit action an admin just took; see
`docs/proxy_hosts.md`'s "Live-managed proxy hosts" section). See
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

New settings:

| Setting | Purpose | Default |
|---|---|---|
| `GEOIP_ENABLED` | Gates the whole feature. | `false` |
| `GEOIP_DB_PATH` | A ready-made `.mmdb` file. Empty = auto-download/cache the free DB-IP city-lite database under `{STATE_DIR}/geoip/`. | `""` |
| `GEOIP_TRAFFIC_POLL_INTERVAL_S` | How often the collector tails the access log. | `60` |
| `GEOIP_ACCESS_LOG_LINES` | Lines requested per poll (Traefik Manager caps this at 1000). | `200` |
| `GEOIP_TRAFFIC_RETENTION_DAYS` | How long `GeoTrafficSample` rows are kept. | `14` |

New modules: `orchestrator.registry.access_log` (the common/JSON
access-log line parser), `orchestrator.registry.geoip` (the IP -> country
lookup, wrapping `geoip2`), `orchestrator.scheduler.geo_traffic_collector`
(the periodic collector + dedup cursor), one new read-only endpoint
(`GET /api/traffic/geo`, `web/routers/geo_traffic.py`) — deliberately bare,
no page template, no nav entry.

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
| `service_state`      | **DB**    | No (runtime)                  |
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

## FK cascade policies

Chosen so YAML edits stay safe and history stays intact:

* `service.node_id` → `RESTRICT` (removing a node while services reference it is a YAML error, not a silent orphan).
* `probe.service_id` → `CASCADE` (probes are meaningless without their service).
* `service_state.service_id` → `CASCADE` (state is per-service).
* `proxy_host.service_id` → `SET NULL` (a route observed for a removed service is still worth keeping until scan re-verifies it's gone).
* `backup_record.service_id` → `SET NULL` + denormalised `service_slug` (backup history survives the service being removed from YAML).
* `pipeline_run.service_id` → `SET NULL` (same reasoning).
* `traffic_sample.proxy_host_id` → `CASCADE` (traffic history for a removed proxy host is not worth keeping as an orphan — unlike `backup_record`/`pipeline_run`, there is no slug to denormalise against).
* `health_scan_sample.service_id` → `CASCADE` (same reasoning — a removed service's health-scan history is not worth keeping as an orphan).
* `geo_traffic_sample.proxy_host_id` → `CASCADE` (same reasoning as `traffic_sample.proxy_host_id` — GeoIP traffic history for a removed proxy host is not worth keeping as an orphan).

## Boot sequence

1. `configure_logging()`
2. Build DB engine (SQLite WAL, `PRAGMA foreign_keys=ON`, 5s busy timeout).
3. If `RUN_MIGRATIONS_ON_START=true`, `alembic upgrade head`.
4. If `services.yaml` exists, run `reconcile_yaml_into_db()`.
5. Build + start the adapter bundle.

The maintenance responder starts *before* step 4 completes so it can serve a
fallback page even if the reconcile fails.
