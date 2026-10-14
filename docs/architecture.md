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
  notifier/{base,null}           # mailcow + ntfy (phase 9)
db/                 SQLModel schema + engine
domain/             enums + Pydantic DTOs shared across modules
health/             probe engine (phase 4)
pipelines/          wake (phase 3), backup (phase 2/6), restore (phase 8)
registry/           YAML <-> DB reconcile + save/reset
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

## Boot sequence

1. `configure_logging()`
2. Build DB engine (SQLite WAL, `PRAGMA foreign_keys=ON`, 5s busy timeout).
3. If `RUN_MIGRATIONS_ON_START=true`, `alembic upgrade head`.
4. If `services.yaml` exists, run `reconcile_yaml_into_db()`.
5. Build + start the adapter bundle.

The maintenance responder starts *before* step 4 completes so it can serve a
fallback page even if the reconcile fails.
