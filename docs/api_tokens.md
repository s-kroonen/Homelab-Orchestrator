# API tokens and privileges

## There are four credentials in play, not two

A common source of confusion: the orchestrator's PBS token is **not** the token
that writes backups. PVE writes backups to PBS itself, using the token stored in
its own storage config. The orchestrator only *observes and curates* the
datastore.

| # | Credential | Configured in | Used by | Does what |
|---|-----------|---------------|---------|-----------|
| 1 | PVE→PBS storage token | PVE storage config | PVE itself | writes backup chunks to PBS |
| 2 | PVE backup token | orchestrator `.env` | orchestrator | triggers `vzdump`, reads tasks, powers guests |
| 3 | PVE restore token | orchestrator `.env` | orchestrator (phase 8 only) | restores to a new VMID |
| 4 | PBS token | orchestrator `.env` | orchestrator | datastore status, list, verify, prune, protect |

Credential #1 already exists in your setup. #2 and #4 are what you need for
phase 2. #3 is not used until the greenlight flow lands.

---

## 2. PVE backup token — routine operations

The orchestrator needs to list guests, start a `vzdump`, poll the resulting
task, and (from phase 3) start/stop guests once a node is awake.

**Privileges:**

| Privilege | Why |
|-----------|-----|
| `VM.Audit` | list guests via `/cluster/resources`, read config |
| `VM.Backup` | run `vzdump` |
| `VM.PowerMgmt` | start/stop guests after a node wake (phase 3) |
| `Datastore.Audit` | see the PBS-backed storage entry |
| `Datastore.AllocateSpace` | write the dump to that storage |
| `Sys.Audit` | read node and task status |

Deliberately **not** included: `VM.Allocate`, `VM.Clone`, `VM.Config.*`,
`Datastore.Allocate`, `Sys.Modify`, `Sys.PowerMgmt`. This token cannot create,
reconfigure, or destroy a guest — worst case it makes a backup you didn't ask
for.

Run on any PVE node as root:

```bash
pveum role add OrchestratorBackup --privs "VM.Audit,VM.Backup,VM.PowerMgmt,Datastore.Audit,Datastore.AllocateSpace,Sys.Audit"
```

```bash
pveum user add orchestrator@pve --comment "homelab-orchestrator control plane"
```

```bash
pveum user token add orchestrator@pve backups --privsep 1
```

That last command prints the secret **once** — copy it into `PROXMOX_TOKEN_SECRET`
before you close the terminal.

### The gotcha that causes most 403s

With `--privsep 1` (privilege separation — the secure choice, and the default),
a token's effective privileges are the **INTERSECTION** of two ACLs:

```
effective = (what the USER may do)  AND  (what the TOKEN may do)
```

So an ACL on only one side grants **nothing**, however generous the role. You
need both rows:

```bash
pveum acl modify / --user orchestrator@pve --role OrchestratorBackup
```

```bash
pveum acl modify / --token "orchestrator@pve!backups" --role OrchestratorBackup
```

Miss either and you get a 401/403 that looks exactly like a wrong secret.
`orchestrator-cli check` surfaces it as `AdapterAuthError` with the server's own
explanation attached.

Verify both rows exist:

```bash
pveum acl list
```

### Narrowing the scope further (optional)

Granting at `/` is simplest. To restrict to specific guests, grant per-VM instead
— but note `/cluster/resources` then only returns those guests:

```bash
pveum acl modify /vms/9001 --token "orchestrator@pve!backups" --role OrchestratorBackup
```

---

## 3. PVE restore token — phase 8 only, higher privilege

Kept separate on purpose: routine backups run daily and unattended, restores are
rare and human-approved. They should not share a blast radius.

**Additional privileges over the backup role:**

| Privilege | Why |
|-----------|-----|
| `VM.Allocate` | create the new VMID a restore lands in |
| `VM.Config.Disk` / `.CPU` / `.Memory` / `.Network` / `.Options` / `.HWType` / `.CDROM` | a restore rewrites guest config |
| `Datastore.Read` | read backup contents back out |

```bash
pveum role add OrchestratorRestore --privs "VM.Audit,VM.Backup,VM.Allocate,VM.PowerMgmt,VM.Config.Disk,VM.Config.CPU,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Config.HWType,VM.Config.CDROM,Datastore.Audit,Datastore.Read,Datastore.AllocateSpace,Sys.Audit"
```

Do not create this token until phase 8. An unused high-privilege token is pure
downside.

---

## 4. PBS token — datastore curation

What the orchestrator actually calls against PBS:

| Adapter method | PBS endpoint | Privilege |
|----------------|--------------|-----------|
| `datastore_status()` | `GET /admin/datastore/{store}/status` | `Datastore.Audit` |
| `list_snapshots()` | `GET /admin/datastore/{store}/snapshots` | `Datastore.Audit` |
| `verify_snapshot()` | `POST /admin/datastore/{store}/verify` | `Datastore.Verify` |
| `prune()` | `POST /admin/datastore/{store}/prune` | `Datastore.Prune` |
| `set_protected()` | `PUT /admin/datastore/{store}/protected` | `Datastore.Modify` |

Note there is **no `Datastore.Backup`** in that list — writing is PVE's job via
credential #1. Nor `Datastore.Read`: the orchestrator never reads backup
*contents*, only metadata.

Run on the PBS host as root:

```bash
proxmox-backup-manager role create OrchestratorDatastore --privs "Datastore.Audit,Datastore.Verify,Datastore.Prune,Datastore.Modify"
```

```bash
proxmox-backup-manager user create orchestrator@pbs --comment "homelab-orchestrator control plane"
```

```bash
proxmox-backup-manager user generate-token orchestrator@pbs datastore
```

Again, the secret prints once — copy it into `PBS_TOKEN_SECRET`.

Scope the ACL to the one datastore, not to `/`:

```bash
proxmox-backup-manager acl update /datastore/YOUR_DATASTORE OrchestratorDatastore --auth-id "orchestrator@pbs!datastore"
```

### If you cannot create custom roles

Creating a role needs `Permissions.Modify`, which a non-admin PBS account does
not have. Use the built-in **`DatastorePowerUser`** instead:

```bash
proxmox-backup-manager acl update /datastore/YOUR_DATASTORE DatastorePowerUser --auth-id "orchestrator@pbs!datastore"
```

It grants more than the orchestrator needs (`Datastore.Backup`,
`Datastore.Read`) and — importantly — **not `Datastore.Modify`**, so snapshot
protection will not work. Set this in `.env`:

```
PBS_PROTECT_ENABLED=false
```

`set_protected()` then becomes a logged no-op instead of a 403.

**What you give up:** the "protected known-good pin" from the design brief —
the guarantee that prune can never remove the newest verified backup of a
service. Without it, retention policy alone decides what survives. That matters
from **phase 6** (retention); phase 2 never calls protect, so nothing is
affected today.

To get it back later you need `Datastore.Modify`, via either a custom role
(needs `Permissions.Modify` on the PBS account) or the built-in
`DatastoreAdmin`. `DatastoreAdmin` on a single datastore path is a reasonable
middle ground — it is scoped to that datastore, not the whole server.

If `DatastorePowerUser` also turns out to lack `Datastore.Verify` on your PBS
version, `orchestrator-cli backup <slug>` will fail at the verify step with
`AdapterAuthError`; run it with `--no-verify` to confirm the rest of the
pipeline works while you sort out privileges.

### PBS has privilege separation too — and it is the #1 cause of 403s

Same intersection rule as PVE, and it catches people from **both** directions:

```
effective = (ACL on orchestrator@pbs)  AND  (ACL on orchestrator@pbs!datastore)
```

Granting `DatastoreAdmin` to the token alone yields nothing if the user has no
ACL — and vice versa. Both auth-ids need a row:

```bash
proxmox-backup-manager acl update /datastore/YOUR_DATASTORE DatastoreAdmin --auth-id "orchestrator@pbs"
```

```bash
proxmox-backup-manager acl update /datastore/YOUR_DATASTORE DatastoreAdmin --auth-id "orchestrator@pbs!datastore"
```

Confirm with `proxmox-backup-manager acl list` — you need rows for the bare user
**and** the `!tokenname` auth-id. A useful reference point: the `pve-backup`
credential PVE uses for writing backups has exactly this pair, which is why it
works.

**The symptom is distinctive:**

| Call | Result |
|------|--------|
| `GET /version` | **200** — needs no privileges, so the token looks valid |
| `GET /access/permissions` | **200 `{}`** — empty: the token has *nothing* |
| `GET /admin/datastore` | **200 `[]`** — sees no datastores |
| `GET /admin/datastore/X/status` | **403** `permission check failed` |

Connectivity and authentication are both fine — `/version` needs no privileges,
which is why the token looks valid right up until the first real call. The
intersection is simply empty.

`orchestrator-cli check` detects this automatically: on a datastore failure it
queries `/access/permissions`, and if the result is empty it prints both
`acl update` commands with your real datastore name and token id filled in. You
should not have to work this out by hand.

---

## Verifying it worked

```bash
DRY_RUN=false orchestrator-cli check
```

| Result | Meaning |
|--------|---------|
| `OK Proxmox VE: version 8.x` | token valid, `/version` reachable |
| `FAIL ... AdapterAuthError` | wrong secret, **or the token ACL is missing** |
| `FAIL ... unreachable` | DNS / firewall / TLS — nothing to do with privileges |
| `FAIL datastore ...` | `PBS_DATASTORE` name wrong, or missing `Datastore.Audit` |

`check` only reads. The first call that needs `VM.Backup` is an actual backup, so
test that on something unimportant:

```bash
DRY_RUN=false orchestrator-cli backup some-unimportant-service --no-verify
```

---

## A note on precision

Proxmox's privilege-to-endpoint mapping shifts slightly between major versions,
and the sets above are deliberately a little generous *inside* the read+backup
envelope rather than minimal to the last privilege. To tighten further, remove
one privilege at a time and re-run `orchestrator-cli check` followed by a real
backup — the adapter reports `AdapterAuthError` naming the failing endpoint,
which makes narrowing straightforward.

What matters most is the boundary that is already firm: **the routine token
cannot restore, clone, reconfigure, or delete a guest.** That is the property
worth protecting.

## Secret hygiene

Both secrets print exactly once at creation. They belong in `.env` (git-ignored)
or Docker secrets — never in the repo, never in `services.yaml`. Rotate with
`pveum user token remove` / `proxmox-backup-manager user delete-token` followed
by a fresh create; the orchestrator picks up the new value on restart.
