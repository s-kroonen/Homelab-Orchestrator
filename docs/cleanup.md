# Cleaning the PBS datastore (cleanup policies)

PBS frees space in two steps:
- **prune** marks old snapshots as gone;
- **garbage collection (GC)** removes the chunks nothing uses any more.

Until GC has run, pruning frees nothing.

PBS has its own prune and GC jobs, but they run at fixed times. A datastore that lives on storage the orchestrator only wakes for backups (a NAS VM on a node that is off most of the day) is offline then, so those jobs fail. The datastore fills up, and every backup fails (OR-95). A **cleanup policy** has the orchestrator do the cleaning while it has the storage awake.

## A cleanup policy

| Field | Meaning |
|---|---|
| `datastore` | The PBS datastore; empty means `PBS_DATASTORE` |
| `schedule_cron` | Its own schedule; empty means it runs only with the backup policies that use it |
| `prune` | Prune each service's snapshots by **its backup policy's** keep rules (the same as after a backup: pin the newest verified one first) |
| `prune_unowned_keep_last` | Also prune groups no policy keeps, keeping the last N. Empty means they are listed in the run and left alone |
| `gc_mode` | `interval` (every run, but not again within `gc_interval_h` of the last one that worked), `above` (only when fuller than `gc_above_pct`), or `off` |
| `max_wait_h` | How long to wait for PBS (default 6). The storage stays on and held all that time. Past it, the GC is stopped in PBS (it starts over next time) and the node is put back |
| `requires`, `wake_mode`, `restore_power_state` | What to wake and hold (the storage VM), as on a backup policy |

A **backup policy** can run a cleanup policy in the same wake:
- `cleanup_policy` names it;
- `cleanup_when` is `before` or `after` the backups.

With `after`, a datastore with less than `PBS_MIN_FREE_BYTES` free has the cleanup run first anyway. A cleanup that fails does not stop the backups.

```yaml
backup_policies:
  - name: daily-frequent
    schedule_cron: "0 2 * * *"
    retention: {keep_daily: 7}
    requires: [storage-vm]
    cleanup_policy: nightly-cleanup
    cleanup_when: before
cleanup_policies:
  - name: nightly-cleanup
    requires: [storage-vm]
    gc_mode: interval
    gc_interval_h: 20
```

## The queue

Backup batches, backups by hand (OR-98), cleanups and verifies still running in PBS run **one at a time, in the order they were asked for**: by a schedule, by *Run now* / *Back up now*, or by the assistant. What a job needs woken is woken once and **carried to the next job while it covers that job's needs**:
- the node is powered on once for a cleanup and the backups after it;
- it is put back only after the last job that needs it (the OR-95 path, with its grace period).

A job that fires while the queue is busy waits its turn and then runs in the same wake. Each job renews the shared hold for as long as it may take.

* `GET /api/backup-queue` shows the jobs running and waiting, and what is held awake. The assistant's tool is `backup_queue`.
* `DELETE /api/backup-queue/{id}`:
  * cancels a waiting job;
  * on a running cleanup, stops its GC in PBS;
  * on a verify being followed, stops waiting for it (PBS finishes it anyway).

  A running backup cannot be stopped here.

## A cleanup run

It shows in Activity as a `cleanup` run, with these steps:
1. `cleanup` (which policy, which datastore, why it ran);
2. `storage` (what the queue woke and holds);
3. `space_before`;
4. `prune` (what was removed per service, the groups no policy keeps, and errors per service);
5. `gc`:
   * while running, it is updated in place with its phase (1 marks the chunks in use, 2 removes the rest) and percentage;
   * at the end, it shows what PBS removed and left pending, or PBS's error;
6. `space_after`;
7. `sync_records` (backup records whose snapshot PBS no longer has are dropped).

A failed cleanup sends `cleanup_failed`.

PBS only removes chunks nothing has used for about 24 hours. So what tonight's prune frees mostly comes back at the next GC: a GC every night, after the prune, keeps that to a day.

## What PBS needs

* The orchestrator's token needs **Datastore.Modify** on `/datastore/<name>`. Datastore.Prune alone only covers groups the token owns, and snapshots Proxmox VE made belong to Proxmox VE's own PBS token. With Privilege Separation on, give the role to the token as well as to its user.
* **Room to clean.** GC writes a little before it frees anything. On a ZFS dataset with a quota, keep the quota a few GB above what is used. If not, GC fails with `EDQUOT: Quota exceeded`, and the fix is to raise the quota for that one run.
* **Turn off PBS's own GC and prune jobs** for the datastore (Datastore, then Prune & GC Jobs). They run while it is offline, and a PBS prune job ignores the backup policies' keep rules.

## On the dashboard

* **Backups → Cleanup**:
  * the queue (what is held awake, what runs, what waits, Cancel / Stop);
  * the cleanup policies (When, Does, Last run, Run now…, Edit, Delete);
  * the datastore card (used, what waits for the next GC, the last GC);
  * a warning when PBS's own GC or prune job would run on it, or the token lacks Datastore.Modify.
* **Backups → Policies**:
  * a backup policy's drawer has a **Cleanup** section (which cleanup policy, before or after);
  * `⋯ → Prune now…` shows PBS's own dry run per service, then queues the prune, with GC after it if wanted.
* **Backups → Snapshots**:
  * each snapshot's service and what made it;
  * **Delete** by hand (a row's `⋯`, or select several), typing the service's name to confirm. A protected snapshot and the newest verified one of a group are never deleted: refused with the reason.
* **Activity**: a cleanup run opens in the run drawer with its steps in words, the GC's progress bar while it runs, and *Stop the GC…*.
* **Sources → the PBS source → Datastores**:
  * usage, the last GC as PBS reports it, and which cleanup policy cleans each datastore;
  * for the chosen one, PBS's own GC, prune and verify jobs, and the token's permissions.

## Endpoints and tools

| What | API | Assistant (MCP) |
|---|---|---|
| List, create, edit, delete | `/api/cleanup-policies` | `list_cleanup_policies` |
| Run now (queued) | `POST /api/cleanup-policies/{name}/run` | `run_cleanup` (needs a person's approval: it deletes backups) |
| The queue | `GET /api/backup-queue`, `DELETE /api/backup-queue/{id}` | `backup_queue` |
| Prune one backup policy now | `POST /api/backup-policies/{name}/prune-preview`, `POST /api/backup-policies/{name}/prune` | `apply_retention` (per service) |
| Delete snapshots by hand | `POST /api/infra/pbs/snapshots/delete` | — |
| One datastore: usage, GC, PBS's own jobs, permissions | `GET /api/pbs-datastore?name=` | — |

Cleanup policies round-trip through `services.yaml` like backup policies, saved by *Save* or the autosave.

Settings:
- `PBS_MIN_FREE_BYTES`: when `after` turns into `before`, and the backup preflight's floor;
- `CLEANUP_POLL_S`: how often a running GC is checked; default 15 s.
