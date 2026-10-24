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

**Back up and update** does the rest, one of three ways (the page uses the best one that is
set up):

### 1. The updater (recommended: checks the new version, rolls back if it fails)

A small sidecar container, [`scripts/updater.sh`](../scripts/updater.sh), that owns the
Docker side. One command installs it - and everything else - in the project directory
(the one with your `docker-compose.yml` and `.env`; no git checkout needed):

```bash
cd ~/orchestrator          # your project directory
curl -fsSL https://raw.githubusercontent.com/s-kroonen/homelab-orchestrator/main/scripts/install.sh | sh
```

(Use `.../dev/scripts/install.sh` instead of `.../main/...` if you follow the `:dev` image.
Prefer to read it first - it can start something that drives Docker - download it, `less`
it, then `sh install.sh`.) It puts four files in place, next to each other:
`docker-compose.yml`, `docker-compose.updater.yml` (the updater add-on),
`scripts/updater.sh` and `scripts/install.sh` itself; **never touches `.env` or `config/`**;
then starts the updater and the orchestrator. Its rules for files:

* a file you changed yourself is left alone: the new version is written beside it as
  `<file>.new` and you are told to `diff` them;
* a file it wrote and you did not touch is updated in place, the old one kept as `<file>.bak`;
* `--ref dev|main|vX.Y.Z|<commit>` picks which version of the files (default: whatever the
  running image follows), `--no-updater` skips the add-on, `--no-start` only places files.

**Updating later is the same one command again** (`sh scripts/install.sh`), the **Update**
button on the System page, or `docker exec orchestrator orchestrator-cli system update`.
When the updater is running, all three go through it, with the safety net below; the script
also refreshes the compose files and the updater first, which the other two do not.

Then **Back up and update** on the System page (or `orchestrator-cli system update`):

1. the orchestrator takes a `pre-update` backup and writes a small request file into the
   shared state volume (`STATE_DIR/update/request`: who asked, which backup to go back to -
   no commands, no image names);
2. the updater keeps the running image under a name that survives the tag moving
   (`orchestrator-rollback:<time>` and `:previous`), pulls the newest build of the **same
   image and tag** the container already runs, and recreates the orchestrator with
   `docker compose up -d` - exactly as you would;
3. it waits for the new container to report **healthy** (`UPDATE_HEALTH_TIMEOUT_S`, 180 s)
   and then to **stay** healthy without restarting (`UPDATE_SETTLE_S`, 45 s), so a crash
   loop that happens to be up for a moment does not pass;
4. **if it does not**: it stops the new container, stages a restore of the `pre-update`
   backup (the new version may have upgraded the database to a shape the old one cannot
   read), and starts the previous image again, then verifies *that* the same way. The
   outcome is `rolled_back` with the reason, and the page shows each step. If the old
   version will not come up either, it says so and tells you what to run on the host.

You get a notification either way: `update_succeeded`, or `update_rolled_back` /
`update_failed` (errors, sent to every channel by default). A run that was already up to
date does nothing and says nothing.

The page follows the run through the restart and reloads when it is done. **Go back to the
previous build** (and `orchestrator-cli system rollback`) undoes an update that *worked* but
turned out wrong: it first takes a `pre-rollback` backup of the current data (so the
rollback itself can be undone), then restores the data from before the update and starts
the previous image.

What rolling back costs: the data goes back to the `pre-update` backup, so anything recorded
between the update and the rollback (health history, traffic samples, notifications) is
lost. That is the price of the old version being able to read its own database.

**The privilege.** The updater can drive the host's Docker (it mounts the socket), which is
root on the host in practice. That is why it is opt-in, runs from a short script you can
read, has no network (`network_mode: none`), no ports and a read-only filesystem, and only
ever acts on the `orchestrator` container: the request can pick a *tag of the image it
already runs* and a *backup file name*, nothing else (both are validated, and the script
never evaluates what it reads). The orchestrator container itself still has no Docker
access. If that is more than you want, use one of the options below.

Notes:

* The updater updates the **image**. Changes to the compose files or the scripts (including
  the updater itself) arrive by running `sh scripts/install.sh` again; the updater service
  is not restarted by its own updates.
* Run compose from the project directory (or set `ORCHESTRATOR_PROJECT_DIR` in `.env` to its
  absolute path): the updater mounts that directory at the same path so `./config`, `.env`
  and friends resolve the way they do for you.
* A container pinned to a digest (`image@sha256:...`) is refused: there is nothing newer.
* One-shot from a shell on the host (the state lives in a Docker volume, so the script runs
  inside the updater container):
  `docker compose -f docker-compose.yml -f docker-compose.updater.yml run --rm updater --update`
  (also `--rollback`, `--tag main`, `--backup <name>`).
* `orchestrator-cli system update-status` shows whether the updater is running and how the
  last run went, step by step.

### 2. A command on the host (no automatic rollback)

`SELF_UPDATE_COMMAND` runs on the Docker host through the [host runner](host_runner.md), the
same way probes reach the host:

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
* Nothing checks the new version: if it does not start, restore the `pre-update` backup and
  start the old image yourself. The updater above does that for you.

### 3. By hand

Without either, the page shows the two Compose commands to run on the host, and still takes
the backup:

```bash
docker compose pull orchestrator
docker compose up -d orchestrator
```

## Restart

**Restart the orchestrator** stops the process (SIGTERM); the Compose policy
`restart: unless-stopped` starts it again. Outside Docker it just stops.

## Settings

| Variable | Default | |
|---|---|---|
| `SELF_BACKUP_ENABLED` | `true` | the schedule |
| `SELF_BACKUP_INTERVAL_HOURS` | `24` | |
| `SELF_BACKUP_KEEP` | `14` | scheduled backups kept |
| `UPDATE_HEALTH_TIMEOUT_S` / `UPDATE_SETTLE_S` | `180` / `45` | the updater: time to become healthy / to then stay up (set in `.env`, read by compose) |
| `SELF_UPDATE_TRANSPORT` | blank | where the update command runs (JSON, like a probe's `transport:`) |
| `SELF_UPDATE_COMMAND` | blank | the update command (split like a shell; no shell is used) |
| `SELF_UPDATE_TIMEOUT_S` | `120` | |
| `UPDATE_REPO` / `UPDATE_BRANCH` | `s-kroonen/homelab-orchestrator` / from the image tag | |
