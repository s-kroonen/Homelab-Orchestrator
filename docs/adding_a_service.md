# Adding a service

Every service is defined by one entry in `config/services.yaml`.  Adding a
new service is a YAML edit — no code changes.

## Minimal example

```yaml
services:
  - slug: my-thing            # unique, kebab-case; used in URLs
    name: My Thing            # human-readable
    description: ""
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
