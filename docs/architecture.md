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
scheduler/          APScheduler wiring (phase 6)
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

## FK cascade policies

Chosen so YAML edits stay safe and history stays intact:

* `service.node_id` → `RESTRICT` (removing a node while services reference it is a YAML error, not a silent orphan).
* `probe.service_id` → `CASCADE` (probes are meaningless without their service).
* `service_state.service_id` → `CASCADE` (state is per-service).
* `proxy_host.service_id` → `SET NULL` (a route observed for a removed service is still worth keeping until scan re-verifies it's gone).
* `backup_record.service_id` → `SET NULL` + denormalised `service_slug` (backup history survives the service being removed from YAML).
* `pipeline_run.service_id` → `SET NULL` (same reasoning).

## Boot sequence

1. `configure_logging()`
2. Build DB engine (SQLite WAL, `PRAGMA foreign_keys=ON`, 5s busy timeout).
3. If `RUN_MIGRATIONS_ON_START=true`, `alembic upgrade head`.
4. If `services.yaml` exists, run `reconcile_yaml_into_db()`.
5. Build + start the adapter bundle.

The maintenance responder starts *before* step 4 completes so it can serve a
fallback page even if the reconcile fails.
