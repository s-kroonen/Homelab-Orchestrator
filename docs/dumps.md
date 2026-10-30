# Dumps: a database backed up on its own

A guest backup (Proxmox -> PBS) is the unit for a root service: it copies a whole VM. A
**sub-service that owns data** - the database in a Portainer stack - can also be **dumped by
itself**, which is smaller, faster, restorable without replacing a guest, and consistent in the
database's own terms.

The orchestrator runs the database's own dump tool **inside its container** (Portainer's exec),
streams the output into a compressed file on the orchestrator host, checks it, records it and keeps
the newest few. It is the dump counterpart of a guest backup, not a replacement: a dump does not
contain the application's files or the OS.

## Setting one up

On the service page, **On / off & deploys -> Dumps** (it appears for a service linked to a Portainer
container), or in the registry:

```yaml
- slug: dev-xymbols-db
  parent: dev-xymbols
  controller: {type: portainer, endpoint: 1, container: dev-xymbols-db-1}
  dump:
    type: postgres          # postgres | mysql | mariadb | mongo | sqlite | command
    database: app           # blank = every database
    schedule: "0 3 * * *"   # cron, in the dashboard's timezone; blank = only when you ask
    keep: 7                 # keep the newest N
    keep_days: 0            # also drop older than N days (0 = no limit)
```

| key | |
|-----|---|
| `type` | which tool runs: `pg_dump` / `pg_dumpall`, `mysqldump` (or `mariadb-dump`), `mongodump --archive`, `sqlite3 .dump`, or your own `command` |
| `database` | one database; blank dumps them all |
| `user` | database user; default is the container's own (`POSTGRES_USER`, root for MySQL) |
| `path` | sqlite only: the database file inside the container |
| `command` | `type: command` only: a shell command run in the container; its stdout is the dump |
| `container` | run in another container than the service's own (same Portainer environment) |
| `schedule` | cron; blank = manual only (or before each deploy, below) |
| `keep`, `keep_days` | retention; see below |
| `timeout_s`, `min_bytes` | give up after this long; a dump smaller than this is a failure |
| `enabled` | `false` pauses it without losing the settings |

**Only on deploys.** A stack whose deploy method keeps the previous version
(`guard.pre.keep_previous`, preset `probes+keep3`; see
[portainer_deploys.md](portainer_deploys.md#deploys)) runs its dump before each redeploy. Leave
`schedule` blank and set `keep` to the number of versions to keep, and the dumps are exactly the
last N releases.

**A whole data folder** (for an app that keeps SQLite and uploads on a volume, without a
`sqlite3` binary in its image) can be a `type: command` dump that writes a tar to stdout, for
example `tar -cf - -C /data db uploads`. SQLite copied while the app writes can be torn, so
prefer a quiet hour, or an app's own export.

From a shell: `orchestrator-cli dump set SLUG ...`, `dump run SLUG`, `dump list`, `dump show`,
`dump clear`. An assistant (MCP) can `list_dumps` and, with the `backup` permission, `run_dump`.

## What a dump does

    check     Portainer set up and writable, the node and guest up (a node that is off is
              reported as that, not as a failed command), the container running, no other dump running
    dump      the tool runs in the container; its output is gzip-compressed as it arrives
    verify    the file reads back whole, is not tiny, and ends the way that tool's output ends
    record    renamed into place; a DumpRecord (size, SHA-256) is written
    prune     the newest `keep` stay; nothing older than `keep_days`

* **Credentials never pass through the orchestrator.** The command is a shell script that uses the
  environment the container already has (`POSTGRES_PASSWORD`, `MYSQL_ROOT_PASSWORD`, ...). Names
  that go into the script (database, user) are limited to letters, digits and `_ . @ -`.
* **A failure leaves nothing behind.** A non-zero exit, a timeout, a corrupt or cut-off file removes
  the partial file and ends the run `failed`, with the tool's own stderr in the message and a
  `dump_failed` notification (on by default). A success sends `dump_succeeded` (off by default).
* **Retention never removes the newest verified dump, or a protected one**, whatever `keep` says -
  so a run of failures can never empty the folder.
* A dump is a pipeline run of kind `dump`: Activity (filter *dumps*), the audit log (`dump.*`),
  push and live updates treat it like a backup.

## Where the files are

`DUMP_DIR` (default `<STATE_DIR>/dumps`), one folder per service:
`<DUMP_DIR>/<slug>/<UTC time>.sql.gz`. Mount a share there to keep dumps off this host. They are
also downloadable from the page (admins only) and listed by `orchestrator-cli dump list`.

**These files are not copied anywhere else.** The orchestrator's own state backup (System page)
covers its database and registry, not `DUMP_DIR`; back that folder up the way you back up
everything else.

## Putting one back

A restore overwrites live data, so it is shown as a command to run on the Docker host, not a
button. The page shows the exact line for the service (for PostgreSQL:
`gunzip -c FILE | docker exec -i CONTAINER sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U ...'`).
Restoring a *guest* is unchanged: the approval page.

## Limits

* The dump runs through Portainer's exec endpoint, which needs a Portainer user allowed to exec
  into the container, and `PORTAINER_READ_ONLY=false`. **Not verified against a real Portainer
  yet** (like the other Portainer writes): try `PORTAINER_READ_ONLY` off with one test database first.
* The dump tool has to exist in the container (`sqlite3` often does not; use `type: command`).
* Redis and other types are not built in - use `type: command`.
* A dump does not wake a node that is off (the guest backup does); it fails and says why.
