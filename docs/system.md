# Managing the orchestrator itself

The dashboard's **System and backups** page (`/system`) and `orchestrator-cli system`
cover what used to be shell commands on the Docker host: what is running, backups and
restores, the database schema, updating, and restarting.

## What it shows

Version and the commit the image was built from, the image tag it follows, uptime,
database size, schema revision against the newest this version knows, and the
backup schedule. The commit comes from the image build (`GIT_SHA`, set by the publish
workflow); the image tag from `ORCHESTRATOR_IMAGE`, which `docker-compose.yml` passes in.

## Backups

One file, `orchestrator-<time>-<why>.tar.gz` in `STATE_DIR/backups`, holding a
consistent copy of the SQLite database (taken with SQLite's online backup, safe while
it runs), `services.yaml`, and a manifest (version, commit, schema revision, checksum).
`.env` is **not** included: it lives on the host and holds secrets. Keep that
somewhere safe yourself.

Backups are taken:

| When | Why tag | Pruned automatically |
|---|---|---|
| every `SELF_BACKUP_INTERVAL_HOURS` (24) | `scheduled` | yes, beyond `SELF_BACKUP_KEEP` (14) |
| you press **Back up now** / `system backup` | `manual` | never |
| before a database schema upgrade (at start, or the **Upgrade** button) | `pre-migration` | never |
| before an update | `pre-update` | never |
| before a restore is applied | `pre-restore` | never |

The pre-migration backup is the safety net for a bad upgrade: if a new version cannot
read the data, restore the `pre-migration` file. `SELF_BACKUP_ENABLED=false` turns off
only the schedule; the others always happen.

Mount `STATE_DIR` (the `orchestrator_state` volume at `/data`) on storage you trust, and
download backups you care about: they sit next to the database they protect.

## Restoring

**Restore** on a backup *stages* it; nothing live changes. The next start (press
**Restart and restore**, or restart the container yourself) takes a `pre-restore`
backup of what is there, then replaces the database and `services.yaml` before the
database opens, and the usual schema upgrade runs on top. A backup from a newer
schema than the running version is refused. **Cancel** drops a staged restore.

Restoring on a brand-new install: put the file in `STATE_DIR/backups` on the host's
volume and run `orchestrator-cli system restore <name>`, then start the container.

## Schema upgrades

`RUN_MIGRATIONS_ON_START` (default on) upgrades the schema when the container starts, with
a `pre-migration` backup first. If it is off, or a migration needs a nudge, the page
shows a banner with **Back up and upgrade**, and `orchestrator-cli system migrate` does
the same.

## Updating

**Check for a newer build** asks GitHub (`UPDATE_REPO`, default
`s-kroonen/homelab-orchestrator`) for the newest commit of the branch the image follows
(`:dev` or `:main`; `UPDATE_BRANCH` overrides), or the latest release for a pinned
`vX.Y.Z` tag, and compares it with the commit the image was built from.

**Back up and update** takes a backup, then runs `SELF_UPDATE_COMMAND` on the Docker
host. The container cannot touch Docker itself (deliberately: no socket mounted), so the
command goes through the [host runner](host_runner.md), the same way probes reach the
host:

```bash
SELF_UPDATE_TRANSPORT={"type": "host_agent", "host": "docker-host"}
SELF_UPDATE_COMMAND=sh -c '(sleep 3; cd /opt/orchestrator && docker compose pull orchestrator && docker compose up -d orchestrator) >/tmp/orchestrator-update.log 2>&1 &'
```

* `docker-host` is the host runner's inventory name for the machine running Compose.
* The runner needs ad-hoc commands on (`allow.command: true`) and `sh` in
  `allow.commands`. That is a real grant: scope it to that one host, and keep the
  command narrow.
* The command should **detach** (the `( ... ) &` above): `up -d` replaces this very
  container, and the request would otherwise be cut off mid-flight. The page then waits
  for the new version to answer and reloads.

Without those two settings the page shows the two Compose commands to run by hand,
and still takes the backup. `orchestrator-cli system update-check` / `update` do the same
from a shell.

## Restart

**Restart the orchestrator** stops the process (SIGTERM); the Compose policy
`restart: unless-stopped` starts it again. Outside Docker it just stops.

## Settings

| Variable | Default | |
|---|---|---|
| `SELF_BACKUP_ENABLED` | `true` | the schedule |
| `SELF_BACKUP_INTERVAL_HOURS` | `24` | |
| `SELF_BACKUP_KEEP` | `14` | scheduled backups kept |
| `SELF_UPDATE_TRANSPORT` | blank | where the update command runs (JSON, like a probe's `transport:`) |
| `SELF_UPDATE_COMMAND` | blank | the update command (split like a shell; no shell is used) |
| `SELF_UPDATE_TIMEOUT_S` | `120` | |
| `UPDATE_REPO` / `UPDATE_BRANCH` | `s-kroonen/homelab-orchestrator` / from the image tag | |
