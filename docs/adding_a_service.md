# Adding a service

Two ways: build it with the CLI (recommended — nothing to hand-edit), or write
the YAML yourself. Both produce the same file; the CLI just validates as it goes.

## With the CLI

```bash
orchestrator-cli scaffold
```

The update command, for after `init`. It offers the guests Proxmox reports that
are not in the file yet (pick with numbers or ranges, `1-5,8,12`), adds new nodes,
and follows guests that moved node. Existing services keep their name, probes and
settings: guests are matched by **VMID**, so renaming a VM does not duplicate it.
Guests that have left the cluster are reported, never deleted. When
`GATEWAY_SERVICE` is set, new services get `depends_on: [<gateway>]`.

On a clean install there is no file to update yet — run `orchestrator-cli init`,
which writes `.env` and `services.yaml` together.

```bash
orchestrator-cli probe add <slug>
```

Walks you through choosing a probe kind and filling in its fields, including the
transport where one is needed. When `PROBE_PROXY_BASE_URL` is set, an HTTP probe
starts from the reverse proxy's local address and the service's proxy hostname,
so reaching a service through the gateway needs no retyping. Then:

```bash
orchestrator-cli scan <slug>
```

shows exactly what the backup gate will decide. If one probe in there is
UNKNOWN or FAILED and the reason isn't obvious from `scan`'s one-line
summary, run just that probe on its own and see the raw result — the exact
URL requested, the status code, the connection error, everything:

```bash
orchestrator-cli probe-test <slug> <probe-name>
```

This bypasses the aggregate verdict and writes nothing to the service's
state — it only runs the one probe and prints what happened. The same view
is available from the dashboard: open the service's page and click **Test**
next to any probe in the list.

`scan` and `probe-test` are both **manual, on-demand** — "debug this one
thing right now." They are not the only way `service_state` gets updated:
a background scheduler (`HealthScanner`, on by default — see
[architecture.md](architecture.md#periodic-scanning-and-the-shared-cache-consistency-guarantee))
scans every enabled service every `HEALTH_SCAN_INTERVAL_S` (default 10
minutes) so the dashboard stays current without anyone running `scan` by
hand, and — unlike any single manual `scan` call — does it with one shared
dependency cache per cycle, so a gateway behind several services is probed
once and every dependent agrees about it. Use `scan`/`probe-test` to debug
a specific service or probe right now; the periodic scanner is what keeps
the whole fleet's state fresh and mutually consistent in the background.
The service page's "Health history" strip shows the periodic scanner's
recent verdicts for that service.

Other commands:

| Command | Does |
|---------|------|
| `service list` | every service, with whether its backup would be allowed |
| `service update <slug>` | change one field (including `--group`); untouched fields are left alone |
| `service remove <slug>` | drop it from the registry (backup history is kept) |
| `probe list [slug]` | probes per service, and which ones gate |
| `probe remove <slug> <name>` | remove one probe |
| `probe-test <slug> <name>` | run one probe now and print its raw result (state, message, details) |
| `proxy add/list/remove <slug>` | record Traefik / Pangolin routes |
| `proxy resync-maintenance` | backfill the gateway's maintenance middleware for every already-linked proxy host — see [proxy_hosts.md](proxy_hosts.md#backfilling-existing-proxy-hosts) |

Everything takes `--non-interactive` plus flags, so the same operations work from
a script or an Ansible task:

```bash
orchestrator-cli probe add haos --non-interactive --kind http --name http-ui --set url=https://haos.lan/ --set verify_tls=false
```

Add `--file PATH` to edit a registry other than the configured one.

### A note on where edits land

These commands edit **`services.yaml`**, never the database. YAML is the saved
source of truth and the DB is rehydrated from it on every boot, so a change
written only to the DB would disappear on the next restart.

The one exception is `proxy resync-maintenance`: it touches only the
gateway's Traefik Manager config (via `sync_maintenance_middleware`), never
`services.yaml` and never a `ProxyHost` row's own fields — there is nothing
for it to write to the registry, so it ignores `--file` entirely.

---

## By hand

Every service is defined by one entry in `config/services.yaml`.  Adding a
new service is a YAML edit — no code changes.

## Minimal example

```yaml
services:
  - slug: my-thing            # unique, kebab-case; used in URLs
    name: My Thing            # human-readable
    description: ""
    group: ""                 # optional, purely organisational — see below
    node: compute-a           # must exist in the top-level `nodes:` list
    guest_kind: vm            # vm | ct | none
    guest_id: 9010            # Proxmox VMID / CTID
    enabled: true
    backup_policy: daily-frequent   # must exist in `backup_policies:`
    probes:
      - name: http-frontend
        kind: http
        required: true
        timeout_s: 5
        order: 10
        config:
          url: "https://mything.example.lan/health"
          expect_status: [200]
```

## Probe kinds

Each probe has `kind`, an optional `required` (default `true`; non-required
probes are diagnostic-only and don't affect the health verdict / gate), a
`timeout_s`, an `order`, and a per-kind `config` dict.

| Kind             | What it checks                                              | Example config keys                                     |
| ---------------- | ----------------------------------------------------------- | ------------------------------------------------------- |
| `http`           | HTTP GET/HEAD, status class, optional body substring.       | `url`, `method`, `expect_status`, `expect_body_contains`, `verify_tls` |
| `tcp`            | Bare TCP connect (used for stacks with no HTTP surface).    | `host`, `port`                                          |
| `systemd`        | SSH + `systemctl is-active <unit>`.                         | `ssh_host`, `ssh_user`, `unit`                          |
| `mqtt`           | Observe a topic within a window.                            | `topic`, `within_seconds`                               |
| `docker_project` | SSH: every container in a compose project is healthy.       | `ssh_host`, `project`, `compose_file`                   |
| `db_redis`       | Trigger `BGSAVE`, run `redis-check-rdb` on the dump.        | `ssh_host`, `dump_path`, `trigger_bgsave`               |
| `db_mongo`       | `mongodump` with `--validate` (WiredTiger integrity).       | `ssh_host`, `mongodump_uri`, `validate_db`              |
| `db_sqlite`      | `sqlite3 PRAGMA integrity_check` against a copy.            | `ssh_host`, `db_path`                                   |
| `db_mariadb`     | `mariadb-check` + `mariadb-dump --single-transaction`.      | `ssh_host`, `mysql_defaults_file`, `databases`          |
| `custom_script`  | Run an executable; exit 0 = pass, non-zero = fail.          | `ssh_host` (optional), `executable`, `args`, `env`      |

## Groups

`group` is a free-form string the operator sets per service, purely
organisational: it has no effect on backups, proxy routing or health.

* `""` (the default) means ungrouped — shown last on the dashboard, under
  "Ungrouped", never hidden.
* The dashboard groups the service table into collapsible sections by it.
* On `/backup-policies`, a "select group" button checks/unchecks every
  service in that group at once in the target-services checklist — the
  submitted request is still the same flat `services: [...]` list the API
  already accepts.
* `/activity` and the dashboard both gain a `?group=` filter alongside the
  existing per-service one.

Set it from the CLI with `orchestrator-cli service update <slug> --group
<name>` (`--group ''` clears it), or from the service's own page in the
dashboard.

## Aggregation rule (the gate)

* All REQUIRED probes HEALTHY -> service is HEALTHY -> backup runs.
* Any REQUIRED probe FAILED   -> service is FAILED  -> backup skipped, alert.
* Otherwise                   -> service is UNKNOWN -> backup skipped, alert only.

DB integrity checks intentionally run against a fresh dump — never live files
— so a live-file lock cannot cause a false FAILED, and a corrupt file cannot
sneak past.

## Save & reset

Edits made in the dashboard live in the DB. The dashboard shows an unsaved
changes badge when the DB differs from `services.yaml`.

* **Save** — write the current DB registry back to `services.yaml`.
* **Reset to saved** — re-run the boot reconcile from `services.yaml`,
  discarding all live edits.

On every boot the DB is rehydrated from `services.yaml` — anything you have
not Saved will be dropped.
