# Waking for a backup

A backup used to check that PBS answered and then dump. Nothing woke anything,
so on a low-power homelab with burst nodes the run simply failed whenever a
node was off. Now every backup starts with a **wake phase**:

```
resolve plan -> hold + wake each entry, in order -> PBS preflight -> integrity gate
             -> vzdump -> verify -> release the holds (always)
```

The wake phase comes **before** the PBS preflight on purpose: the PBS datastore
may live on NFS served by a VM on a normally-off node, and PBS is not reachable
until that VM is up.

## Two separate needs

| Setting | Where | Meaning |
| --- | --- | --- |
| `requires` | backup policy | Services that must be **up** for *every* backup under the policy (e.g. the storage VM). |
| `backup_requires` | service | Extra services needed by *this one* service's backup (e.g. its database service). |
| `wake_before_backup` | backup policy (default **true**) | Also wake the services being backed up: node on, guest running, probes healthy. |

### Not `depends_on`

`depends_on` is a **health-gate** dependency: if the gateway is down, a
downstream verdict becomes UNKNOWN instead of FAILED. It never wakes anything and
is not consulted here. `requires` / `backup_requires` are the opposite: they
*wake* and *hold* things, and have no effect on health verdicts. A service can
have both, for different reasons.

## The plan

The plan is the ordered list of services a backup wakes:

1. the policy's `requires`, in the order written,
2. the service's own `backup_requires`,
3. the service itself, if the policy has `wake_before_backup` (a service with no
   policy is treated as on).

A service named twice is woken once, at its **first** position. Slugs must exist,
a service may not list itself, and the requirements may not form a cycle (through
other services' `backup_requires` or the policy's `requires`). The same checks run
when the YAML loads, in the HTTP API and in the CLI.

**Requires are not transitive.** Each required service is woken through its own
node and guest only; *its* `backup_requires` and its policy's `requires` are not
followed. If the storage VM needs something else up, list that something in the
policy too.

A required service may be `backup_excluded` — the storage VM usually is, because
it hosts the pool PBS lives on. That is fine and expected: exclusion stops it
being *backed up*, not being *woken*.

**A `backup_excluded` service can never be in a policy** (TrueNAS, or whatever hosts the PBS
storage): the policy form shows it greyed out as "never backed up", the API and the bulk
"set policy" action refuse to put it in one or to name it in a policy's targets, the CLI/editor
refuses to save a file that does, and a scheduled run never resolves it as a target. Marking a
service as excluded takes it out of the policy it was in. (A file written before this rule still
loads - the exclusion wins and a warning is logged - but it cannot be saved again until the
contradiction is removed.) The backup pipeline itself still refuses it unconditionally, with no
override.

### Previewing it

```bash
orchestrator-cli backup plan example-media            # ordered plan; exit 1 on a blocker
orchestrator-cli backup plan example-media --policy monthly-full
curl -b cookie 'https://orch/api/backup/plan?slug=example-media'
```

The API answers `{slug, policy, steps: [{slug, name, node, reason, node_power,
blocker}], blockers, will_wake, skips, timeline}`. `skips` lists what would make a backup
be skipped (see *Skipped, not failed* below); `timeline` is how long the wake phase is likely
to take, step by step, and the boot order of every node it would power on, with the guests
it needs marked (see [boot_order.md](boot_order.md)). A **blocker** means the node has no
`power_mgr_target` and is not `always_on`, so the orchestrator has no way to turn
it on. The service page's **Backups** tab shows the same list as the *Backup plan*
panel, with blockers in red, and the policy form warns before you save.

A blocker only matters when a wake is actually needed. A blocked node that is up
right now is simply up, and the backup proceeds.

## What the wake phase does

For each plan entry, in order:

1. **Hold.** Assert a power-manager hold on the entry's node (skipped for
   `always_on` nodes and nodes with no power target — they are never idle-shut-down).
   One hold per node per run, however many entries share it.
2. **Wake.** If the node is online and the guest running, nothing is fired.
   Otherwise the existing wake pipeline runs (`WakePipeline.ensure_awake`):
   power on, wait for the node, start the guest, wait until its probes are HEALTHY.
   It goes through `get_or_start`, so a backup needing a node that a visitor (or
   another backup) is already waking **joins that wake** instead of firing a second
   one, and a wake that just finished inside `WAKE_RETRY_COOLDOWN_S` is reused.
   A wake is never joined when nobody can finish it: at start-up the orchestrator
   closes every run the previous process left running ("interrupted"), a pipeline
   that hits an error it did not expect closes its own run, and a wake still marked
   running after `2 × (WAKE_TIMEOUT_S + WAKE_GUEST_START_TIMEOUT_S)` is closed as
   "abandoned" and a fresh one is started. Before this, one stuck wake failed every
   backup that needed its node, every night, without trying.
3. **Record** one step: `wake_prerequisite:<slug>` or `wake_target:<slug>` with the
   reason, the node and whether it was woken or already up.

Holds last until the **whole backup** ends and are released in a `finally` —
success, failure or cancellation — so the manager's idle shutdown cannot pull the
TrueNAS node out from under a running dump. Their TTL is long enough for a wake
plus the guest's dump and verify deadlines (see *How long a dump and a verify may take*), so
a crashed orchestrator cannot pin a node forever.

If nothing needs doing — no prerequisites, and the target's node is already up or
always on — the phase records nothing and holds nothing: such a backup is
unchanged from before.

### Failure semantics

A wake that fails or times out **fails the backup run at that step**, with an
error naming the service and node. The PBS preflight, the integrity gate and the
dump never run, so the last known-good backup is untouched — the same philosophy
as the gate. `backup.failed` is audited as always. Nodes that can never be woken
fail up front, before earlier entries power anything on, with:

> … cannot be woken: no power target configured (set the node's power target or mark it always on)

Note that a *target* that needs waking must become HEALTHY as part of the wake. A
service that is already running and unhealthy is not waked at all; the gate then
judges it (FAILED vs UNKNOWN) as before.

### Skipped, not failed

Some backups cannot be taken, and nothing went wrong: the service is unhealthy, nothing can
check it, or it is off and this backup may not wake it. Those runs end **skipped** (a
`skipped` step with the reason, the run status `skipped`, `backup.skipped` in the audit log
and the Activity feed, a `backup_skipped` notification - not sent by default). The last
backup is kept, and nothing is pruned. They are decided as early as possible:

| Why (`why`) | When | Tried again? |
|---|---|---|
| `no_checks` | the service, or a sub-service its backup waits for, has no enabled **required** probe, so the gate could only ever say UNKNOWN. Decided before anything is woken. | no |
| `unwakeable` | its node is off (or its guest stopped) and the backup may not wake it: no power target, the node's wake on hit switch is off under wake mode `follow`, wake mode `never`, or *Wake before backup* off. Decided before anything is woken. | no |
| `unhealthy` | the gate scanned it and it is FAILED. A restore candidate. | no |
| `unknown` | the gate scanned it and could not tell (a probe that could not run). | yes, while tries are left |
| `sub_unhealthy` | a required sub-service is FAILED or UNKNOWN. | only when UNKNOWN |

A *prerequisite* (the storage VM) that cannot be woken is not a skip: it **fails** the run,
because every backup of the policy is stuck behind it. A guest that was started and did not
come up (DOWN) also fails, and is retried.

`POST /api/backup/SLUG` answers **409** `Backup skipped: …` for a skip (400 for a failure);
`orchestrator-cli backup SLUG` prints *Backup skipped* and exits 3. A scheduled policy run
records `"backup": "skipped"` for that service and carries on with the rest.

### The gate always requires a running guest

The integrity gate's scan starts with the **guest gate** (see
docs/architecture.md, "Health model"): node online and guest running. That
check is not optional for a backup — with `wake=false`, with the policy's
*Wake before backup* off, or after a wake that did not bring the guest up, a
stopped guest makes the verdict DOWN and the gate refuses:

> integrity gate refused 'example-media': the guest is not running (DOWN) — guest vm 9001 is stopped. … This is NOT a restore candidate. Turn on 'Wake before backup' in the backup policy (or start the guest) so the backup brings it up first.

The wake phase runs before the gate, and the gate reads the guest state fresh
from Proxmox (never a scan cycle's cached copy), so a guest the wake just
started is seen running. Only `gate=False` / `--no-gate` bypasses it, as before.

### Skipping it

`orchestrator-cli backup SLUG --no-wake` and `POST /api/backup/SLUG?wake=false`
skip the whole phase (prerequisites and target) for the case where you brought
everything up by hand. The skip is recorded as a `wake_prerequisites` step with
status `skipped` and audited as `backup.wake_skipped`, like `--no-gate`.

## Scheduled policies

A policy run backs up several services behind the same prerequisites. The
scheduler wakes the **policy-level** prerequisites **once**, holds them for the
whole batch and releases them after the last service. Per-service extras and each
target are woken per service, in that service's own run. Each run still shows a
`wake_prerequisite:<slug>` step (marked `shared`), and one service failing does
not stop the rest.

If the shared prerequisite cannot be woken, every service in the batch fails at
that step with the same error — and the wake is **not** retried per service.

> **The cron time is when the wake *starts*.** A TrueNAS VM that takes six
> minutes to boot means the first dump starts six minutes after `0 3 * * *`. Give
> the schedule headroom, and make sure `WAKE_TIMEOUT_S` (default 600 per entry) is
> long enough for the slowest node.

### Pruning old backups (retention)

After each service's **successful scheduled** backup, its policy's `retention` (`keep_last`,
`keep_daily`, `keep_monthly`, `protect_latest_verified`) is applied to its PBS backup group: the
newest verified snapshot is pinned first when the policy says so, then PBS's own prune removes
what the counts do not keep (`scheduler/retention.py`). A failed or skipped backup prunes nothing,
and backups of guests the orchestrator does not manage (or made by hand in Proxmox) are never
touched. Pruning only frees the space after PBS's **garbage collection** runs on the datastore:
keep a GC schedule there.

When PBS refuses a prune, the reason PBS gives is in the audit entry and in a **job_failed**
warning (*Pruning old backups of <slug> failed*, [notifications.md](notifications.md)); it is
sent once, again only when the error changes or after a day, and *recovered* follows when a prune
works again. To check or catch up without waiting for the night, the assistant has
`retention_preview` (PBS's dry run: what would be removed, or why PBS refuses; changes nothing)
and `apply_retention` (the same run as the night's; always a person's approval).

## Manual backups

The dashboard's **Back up now**, `POST /api/backup/{slug}`, the bulk *Back up* action and the
assistant's `run_backup` go through **the backup queue** (OR-98), like the policies' batches
and the cleanups ([cleanup.md](cleanup.md#the-queue)):

* the call answers at once: `{"queued": true, "job_id", "run_id" (once it started), "status":
  "running" | "queued", "ahead": jobs before it}`;
* it runs behind what runs now, in the same wake: a backup by hand while the storage is up for
  a cleanup does not power it on twice;
* the service page follows the run (`GET /api/runs?slug=…&limit=1`) and shows its steps live.

**One backup per service at a time.** A second one while one is running or waiting is refused
with 409 and the run it would duplicate. A policy's batch that reaches a service being backed
up by hand skips it (`already_running`); that one counts. Before, two clicks a second apart
started two dumps (Proxmox ran them one after the other) and two verifies that shared the disk,
each at half the speed, and both runs failed on the verify's deadline.

`?wait=true` runs it outside the queue and answers when it has finished, and
`orchestrator-cli backup SLUG` does the same; so does the API when the scheduler is off.

## How long a dump and a verify may take

Each guest's deadline is learned from its own runs (`pipelines/backup_timing.py`):

| | |
|---|---|
| History | the last 5 dumps (`vzdump_finished`) or verifies (`pbs_verify`) that worked |
| Deadline | `BACKUP_DEADLINE_FACTOR` (2) x the slowest of them + 5 min |
| Bounds | never under `BACKUP_DEADLINE_MIN_S` (10 min), never over `BACKUP_TASK_MAX_S` (12 h) / `VERIFY_TASK_MAX_S` (6 h) |
| No history yet | `BACKUP_TASK_TIMEOUT_S` (2 h) / `VERIFY_TASK_TIMEOUT_S` (30 min); a verify at least the snapshot's size read at `VERIFY_MIN_MIB_S` (15 MiB/s) |

So a container that verifies in a minute gets 10 minutes, and a big VM on slow storage gets
what it has needed before, times two. The deadline and why are in the step's detail
(`vzdump_started`, `pbs_verify`: `deadline_s`, `deadline_why`, `history_s`). The holds a
batch takes are sized from the same numbers.

**A verify past its deadline is not a failed backup.** The backup is in PBS; only its check is
not done:

1. the run ends **succeeded**, its `pbs_verify` step `running` with PBS's task id, and the
   record `verify_state: running`;
2. a `verify` job in the queue follows PBS's task. While it waits, the storage it needs stays
   held (the queue carries the wake to it), so the node is not powered off under PBS;
3. when PBS finishes, the record and the step say what PBS said. A failed verify fails the run
   then and sends `backup_failed`;
4. past `VERIFY_TASK_MAX_S` from its start (or *Stop waiting* on the queue card) the record is
   `verify_state: unknown` and the queue moves on. PBS finishes it anyway.

After a restart, verifies still being followed are followed again (nothing is woken for
them). A backup's snapshot listing and a cleanup's `sync_records` also mark records verified
when PBS lists their snapshot as verified, so a verify the orchestrator stopped waiting for,
or one PBS's own verify job ran, counts.

## Example: TrueNAS behind PBS

```yaml
nodes:
  - name: storage
    always_on: false
    power_mgr_target: storage          # something the power adapter can turn on

backup_policies:
  - name: daily-frequent
    schedule_cron: "0 3 * * *"
    requires: [truenas]                # NFS for the PBS datastore lives here
    wake_before_backup: true

services:
  - slug: truenas
    node: storage
    backup_excluded: true              # never backed up to its own pool ...
    backup_excluded_reason: "Hosts the pool PBS lives on"
    # ... but it is woken and held for every backup.
  - slug: nextcloud
    backup_policy: daily-frequent
    backup_requires: [nextcloud-db]    # this service also needs its database up
```

Edit it without touching YAML:

```bash
orchestrator-cli policy update daily-frequent --requires truenas
orchestrator-cli policy update daily-frequent --no-wake-before-backup
orchestrator-cli service update nextcloud --backup-requires nextcloud-db
orchestrator-cli policy list      # shows REQUIRES, flags ones that cannot be woken
orchestrator-cli service list     # NEEDS column; "blocked" if a prerequisite cannot be woken
```

In the dashboard: **Backup policies → Edit** has the *Services that must be up*
picker and the *Wake the services being backed up first* toggle; the service page's
**Backups** tab has *Extra requirements* and the *Backup plan* panel.

## What it does not do (yet)

Nothing about wake-on-hit: a node woken by a *visitor* is still switched off by whatever
manages idle shutdown on your side (the orchestrator only puts back what a *backup* changed).

## Wake mode, retries and putting things back

Set per backup policy (Backup policies -> Edit, or `orchestrator-cli policy update NAME`):

* **Wake switches** (`wake_mode`): `follow` (default) - a backup does not wake a *node* whose
  *wake on hit* switch is off, and says so in the plan and the run; `always` - wake it anyway,
  the backup is your own request; `never` - wake nothing and use whatever is up.
  A *service's* wake-on-hit switch never stops a backup: it is there so a background client (a
  phone syncing, a crawler) does not wake that service, and a backup is not a visitor. So a
  storage VM with wake on hit off is still woken as a policy prerequisite, on a node that may
  be woken. An always-on node is never woken, so its switch never blocks anything either.
* **Tries** (`max_attempts`, default 3): a scheduled run that is *blocked* (a prerequisite
  could not be woken) or whose target is *not healthy after boot* is tried again after
  `BACKUP_RETRY_DELAY_S` (default 5 min), up to this many times. You get a `backup_retry`
  notification for each failed try that will be repeated and one `backup_failed` ("after 3
  tries") when it gives up - then it waits for the next scheduled run. A health check that
  could not run (UNKNOWN) is a skip that is tried again: logged on each try, one
  `backup_skipped` at the end. A service that is definitively FAILED, has no required check
  or may not be woken is skipped once; a failed dump or a failed verify are never retried.
* **Boot order**: after the backup powers a node on, guests that Proxmox starts at boot are
  left to Proxmox (the run waits for them in their place in the boot order); the others are
  started. See [boot_order.md](boot_order.md).
* **Put it back afterwards** (`restore_power_state`, default on): the backup remembers what
  it found. Guests it had to start are stopped again; a node it powered on is powered off
  (graceful) after `BACKUP_POWER_OFF_GRACE_S`. A node that was already on stays on. A node is
  left alone - and the step says why - when something else still holds it, when someone
  else woke a service on it during the backup, or when it is always-on / has no power target.
  A scheduled batch powers the node off once, after its last service; the batch has no run
  of its own, so that step is in the activity feed as `backup.restore_state`. This holds
  whether the backups worked or failed. If powering off fails you get a
  `power_restore_failed` notification; a node the backup powered on and leaves on because
  something else still needs it gives a `power_restore_skipped` warning ("was left on after
  the backup") with the reason.
* **Holds that outlive their run**: every hold the orchestrator counts on a node carries an
  expiry (its TTL). One past it no longer counts as "something else still holds it", and at
  start-up every hold is dropped (`node.holds_cleared` in the activity feed): the runs that
  took them did not survive the restart. Before this (OR-95), a wake cut off by a restart left
  its hold counted for good, and from then on no backup powered that node off again.

### When PBS is full

The PBS preflight reads the datastore's free space. With less than `PBS_MIN_FREE_BYTES`
(default 1 GiB; 0 turns the check off) the backup stops there, before the integrity gate and
the dump, with an error that says how full it is. Its node is put back as after any failure.
Before, the dump ran and failed part-way with Proxmox's "job errors"; a failed dump now
carries the error lines of Proxmox's task log ("Proxmox: ERROR: ... no space left on device").

Pruning old snapshots marks them removed; the space only comes back after PBS's garbage
collection, which needs the datastore online. A **cleanup policy** does both while the
orchestrator has the storage awake, before or after a backup policy's backups in the same wake:
see [cleanup.md](cleanup.md).

Before this, nodes were only *held* during a backup and then released, relying on the power
manager to switch them off; the MQTT power manager has no idle shutdown of its own, so a node
woken for a 05:00 backup stayed on.
