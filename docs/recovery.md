# Automatic recovery and restores

Recovery is **off** unless a service has a `recovery:` block (the **Recovery (JSON)**
box on its page, or `services.yaml`). A global switch, `RECOVERY_ENABLED`, turns the
whole thing off.

```yaml
recovery:
  steps: [restart, restart_parent, propose_restore]
  max_attempts: 3      # per window; default = the number of steps
  window_s: 3600       # the attempt counter resets after this long
  settle_s: 120        # wait this long after an attempt before the next one
```

## The ladder

When the service is **FAILED** on `RECOVERY_CONFIRM_SCANS` (2) consecutive health
scans, the next step is tried, one per `settle_s`:

| Step | Does |
|---|---|
| `restart` | restart *this* service through its controller ([controllers.md](controllers.md)) |
| `restart_parent` | restart the nearest parent that has a controller |
| `restart_guest` | restart the root's whole Proxmox guest — a bigger hammer, so you have to list it |
| `propose_restore` | create a restore proposal and alert you with an approve link (below) |

A step that cannot apply (no controller, no parent controller, no guest) is skipped.
Repeat a step to retry it: `steps: [restart, restart, restart_guest]`. Each attempt is
announced (`recovery_attempt`), audited (`recovery.<step>`) and recorded; when the
ladder runs out inside the window it says so once (`recovery_exhausted`) and leaves the
service alone until it is healthy, the window passes, or you press **Reset recovery**.
When a service comes back after an attempt, `recovery_succeeded` is sent.

## What it will not do

* act on DOWN (stopped, node off) or UNKNOWN — those are not failures; wakes handle DOWN;
* touch a child while any ancestor is not healthy (the root cause is dealt with first);
* touch anything while the root is being backed up;
* restore anything by itself — a restore is only ever **proposed**.

## Restores

A restore overwrites the guest with an older copy, so it needs a person:

1. A proposal is created — by the `propose_restore` step, or **Propose a restore…** on a
   service's Backups tab. It names the latest **verified** backup of the root service and
   is valid for `APPROVAL_TTL_HOURS` (24). A newer proposal supersedes an open one.
2. The alert carries a link to `/approvals/<token>` (built from `EXTERNAL_URL`, else
   `ORCHESTRATOR_PUBLIC_URL`). You need to be logged in.
3. **Approve and restore** stops the guest, restores the backup over it, starts it and
   waits for the service to be healthy, with progress on the page. **Deny** closes it.

It needs the restore token (`PROXMOX_RESTORE_TOKEN_ID` / `_SECRET`, see
[api_tokens.md](api_tokens.md)); the routine token deliberately cannot do this. A
sub-service cannot be restored on its own — propose it for its root.
