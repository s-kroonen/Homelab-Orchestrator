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
blocker}], blockers, will_wake}`. A **blocker** means the node has no
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
3. **Record** one step: `wake_prerequisite:<slug>` or `wake_target:<slug>` with the
   reason, the node and whether it was woken or already up.

Holds last until the **whole backup** ends and are released in a `finally` —
success, failure or cancellation — so the manager's idle shutdown cannot pull the
TrueNAS node out from under a running dump. Their TTL is long enough for a wake
plus a dump plus a verify, so a crashed orchestrator cannot pin a node forever.

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

## Manual backups

The dashboard's **Run backup now**, `POST /api/backup/{slug}` and
`orchestrator-cli backup SLUG` go through the same path and so wake too. The
HTTP call is still synchronous (it returns the finished run); because a cold
storage node can take minutes, the run row is created first and every step is
committed as it happens, so the service page polls `GET /api/runs?slug=…&limit=1`
and shows the wake steps live. No database write transaction is held open while a
node boots.

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

* **Wake switches** (`wake_mode`): `follow` (default) - a backup does not wake a service or
  node whose *wake on hit* switch is off, and says so in the plan and the run;
  `always` - wake it anyway, the backup is your own request; `never` - wake nothing and use
  whatever is up.
* **Tries** (`max_attempts`, default 3): a scheduled run that is *blocked* (something could
  not be woken, or the switch above) or whose target is *not healthy after boot* is tried
  again after `BACKUP_RETRY_DELAY_S` (default 5 min), up to this many times. You get a
  `backup_retry` notification for each try that will be repeated and one `backup_failed`
  ("after 3 tries") when it gives up - then it waits for the next scheduled run. A service
  that is definitively FAILED, a failed dump or a failed verify are never retried.
* **Put it back afterwards** (`restore_power_state`, default on): the backup remembers what
  it found. Guests it had to start are stopped again; a node it powered on is powered off
  (graceful) after `BACKUP_POWER_OFF_GRACE_S`. A node that was already on stays on. A node is
  left alone - and the step says why - when something else still holds it, when someone
  else woke a service on it during the backup, or when it is always-on / has no power target.
  A scheduled batch powers the node off once, after its last service. If powering off fails
  you get a `power_restore_failed` notification.

Before this, nodes were only *held* during a backup and then released, relying on the power
manager to switch them off; the MQTT power manager has no idle shutdown of its own, so a node
woken for a 05:00 backup stayed on.
