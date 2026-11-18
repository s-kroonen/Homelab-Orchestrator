# The QEMU guest agent: what each VM reports from inside

Proxmox can tell whether a VM runs, but not what happens inside it. The QEMU guest agent can:
the VM's own addresses, who is signed in, how full its disks are, whether a backup left it
frozen. The orchestrator **only reads** from it, every health cycle, and shows the result in the
existing **Guests** tab of each Proxmox source. There is no new page and nothing new to operate.

## What you see

**Proxmox source → Guests.** Next to state, start with node, disk and service:

| Column | Shows |
|---|---|
| Address | the guest's own address (the NIC whose MAC Proxmox knows). When the agent gave none, the address its service found another way, marked `ARP`, `DHCP` or `LXC` |
| Agent | `answers`, `not answering`, `no permission`, `commands on` (not locked down), *Set up agent…* (the option is off), or `not needed` (containers) |
| Signed in | each signed-in user and how long they have been signed in |

A frozen guest gets a red **frozen** pill next to its state. The filters add **Agent needs a
look**, **Signed in** and **Frozen**. When Proxmox refuses to let the orchestrator read some
guests' agents, a red banner above the table says so, with *How to fix*.

**A guest's drawer** (click its name, or *Details…* in its ⋯ menu; the URL gets
`?guest=node/vmid`, so alerts link straight to it):

* agent state and when it was read, with **Read now** (reads that one guest at once; at most
  once per 30 s per guest);
* **Signed in**: user, since, and for how long;
* **Network**: every interface with its addresses and traffic since the guest booted;
* **Disks**: every filesystem with how full it is; network mounts (NFS, SMB) listed as not
  measured;
* **Backups and agent**: frozen or thawed after the last backup, locked down or not, agent
  version and answer time;
* **Proxmox**: start with node, freeze on backup.

When the agent cannot be read, the drawer shows only the steps still missing (see Setup) instead
of the readings. The drawer is read-only: start, stop and boot settings stay in the row's ⋯ menu.

**A service** shows none of this, only a link: where it says what it runs on (`VM 102 on eve1`),
the guest is a link to its drawer, with a ⚠ while that guest has open alerts (agent, disk, guest
down). A sub-service links to its parent's guest.

**Assistants** get the same per-guest fields in `list_guests` (read scope).

## What the orchestrator asks the agent

Seven read-only commands, and nothing else:

| Command | Gives |
|---|---|
| `ping` | does the agent answer (the operating system is up, not just the VM process) |
| `info` | agent version, and which commands are switched on: the lock-down check |
| `network-get-interfaces` | addresses, MACs, traffic counters |
| `get-users` | who is signed in, since when |
| `fsfreeze-status` | frozen or thawed |
| `get-fsinfo` | filesystems and how full they are |
| `get-osinfo` | OS name, version, kernel (read once a day) |

Never sent: exec, file read or write, password changes, freeze, thaw, trim, shutdown, suspend.

## Security

* **Read-only by construction.** `adapters/proxmox/agent.py` holds the allow-list above. Every
  agent call goes through `agent_read`, which refuses any other command before a request
  leaves the orchestrator. `tests/test_guest_agent.py` pins the list: adding a command is a
  reviewed change.
* **Least privilege.** The orchestrator's existing Proxmox token needs only
  `VM.GuestAgent.Audit` (Proxmox 9; `VM.Monitor` on Proxmox 8, which is broader: there the
  lock-down below matters more). No second token.
* **Locked down in the guest, and checked.** The setup steps switch exec, file, password and
  SSH-key commands off in the agent itself (`block-rpcs`), which protects the guest even from a
  stolen or over-privileged token. The agent's `info` shows whether it took effect; a guest
  where those are on shows **commands on** and raises `lockdown_weakened`.
* **Read now is the only new write route**, and it only triggers a read: signed-in admins only,
  once per 30 s per guest.
* **A hung agent costs little.** 5 s per call (`AGENT_TIMEOUT_S`), 4 guests at a time, 30 s
  per guest in all. A guest whose agent misses two reads in a row is read every third cycle
  until it answers again (Read now still reads it at once).
* **Little is kept.** Only the latest reading per guest (`guest_facts` table). Changes go to
  the notification feed like any other alert.

## Setup

Once, on a Proxmox node: let the orchestrator's token read agents (the role is the one its
token uses; see [api_tokens.md](api_tokens.md)):

```
pveum role modify OrchestratorBackup --append 1 --privs "VM.GuestAgent.Audit"
```

Per VM (the drawer shows these, fitted to the VM's OS, and only the ones still missing):

1. Install it in the guest: `apt install qemu-guest-agent` (Fedora/RHEL `dnf`, Alpine `apk`;
   Windows: the VirtIO guest tools).
2. Lock it down:
   ```
   printf '[general]\nblock-rpcs=guest-exec,guest-exec-status,guest-file-open,guest-file-close,guest-file-read,guest-file-write,guest-file-seek,guest-file-flush,guest-set-user-password,guest-ssh-add-authorized-keys,guest-ssh-remove-authorized-keys,guest-ssh-get-authorized-keys\n' > /etc/qemu/qemu-ga.conf
   systemctl restart qemu-guest-agent
   ```
   Check the names on your version with `qemu-ga --block-rpcs=help`.
3. Proxmox: VM → Options → QEMU Guest Agent → *Use QEMU Guest Agent* and *Freeze/thaw guest
   filesystems on backup*.
4. Stop and start the VM once (the option adds a device), then **Read now**.

## What the states mean

| Agent | Means | Shown as |
|---|---|---|
| answers | it answered `ping` | `answers` (or `commands on` when not locked down) |
| not answering | the option is on but nothing answers: not installed, not running, or the guest is stuck | `not answering` |
| no permission | Proxmox refused the read (403): the token lacks `VM.GuestAgent.Audit` on that VM | `no permission`, and the banner |
| not enabled | the VM's QEMU Guest Agent option is off | *Set up agent…* |
| not needed | a container: Proxmox reads it itself | `not needed` |
| not running | the guest is stopped | — |

## Alerts

Level **agent**, keyed by the guest (`node/vmid`), routed and muted by rules like any other:

States, announced when they change (confirmed like every change) and `recovered` when they
clear:

| Event | When | Severity |
|---|---|---|
| `agent_unresponsive` | the guest runs, its agent answered before, and now it does not | warning |
| `lockdown_weakened` | the agent accepts exec, file or password commands | warning |
| `network_down` | the agent answers, but the guest's own network card (the MAC Proxmox knows) is gone or has no address | error |
| `guest_frozen` | its filesystems are frozen: writes are paused, usually by a backup that did not thaw them. The alert says how to thaw them | error |

Moments, sent once, comparing a reading with the one before it:

| Event | When | Severity | Sent by default |
|---|---|---|---|
| `guest_signin` | someone signed in (user, since when) | info | yes |
| `guest_signout` | someone signed out, with how long they were signed in (to within one reading) | info | no: in the feed, sent when a rule asks |
| `ip_changed` | the guest's own address changed | info | yes |

The first reading after the agent was set up, or came back, announces no sign-ins. *Read now*
sends the moments it finds too, so a sign-in it sees is not lost. A guest without the agent
never alerts. Disk space keeps its own `disk_low` / `disk_full` ([disks.md](disks.md)), read
from the same reading.

**After a backup.** Right after the orchestrator backs up a VM (whether the dump succeeded or
not), it asks the agent once whether the filesystems were thawed again, records it on the
backup run as a `freeze_check` step (`ok`, or `failed` with the command that thaws them), and
hands it to the frozen check at once. The health cycle checks every VM too, so a backup that
Proxmox runs on its own schedule is covered as well.

## IP addresses

[Service IP discovery](service_ips.md) takes a VM's address from its latest reading when it is
recent (30 minutes), so the agent is asked once per cycle, and says *why* there is none: the
option is off, Proxmox refused, or the agent does not answer. With no recent reading it asks
Proxmox as before; ARP and DHCP stay the fallback.

## Guests that are not Proxmox VMs (later)

Nothing outside `guests/providers/` assumes Proxmox. A guest is a `GuestRef` (source, key),
what is known about it is `GuestFacts`, and whoever reads it is a `GuestFactsProvider`
(`guests/model.py`). A plain host with its own agent becomes a second provider; the collector,
store, checks, IP discovery and the drawer stay as they are. Services find their guest through
one place (`guest_link` in `web/routers/views.py`).

| Module | Job |
|---|---|
| `adapters/proxmox/agent.py` | the allow-list |
| `guests/model.py` | `GuestRef`, `GuestTarget`, `GuestFacts`, the provider protocol |
| `guests/providers/proxmox.py` | reads a Proxmox guest (config, then its agent) |
| `guests/collector.py` | reads all guests on time, with the back-off |
| `guests/store.py` | the latest reading per guest |
| `guests/checks.py` | readings → alerts (one class per check) |
| `guests/setup_guide.py` | the checklist and missing steps |
| `guests/view.py` | what the screens and assistants get |
| `guests/runner.py` | wires them together for the health cycle and Read now |
