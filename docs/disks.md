# Guest disks: a warning before one runs full

Every health cycle (10 minutes) the orchestrator reads how full each **running** guest's
filesystems are, shows it on **Nodes & guests**, and alerts when one is filling up.

## Where the numbers come from

| Guest | Read from | What you see |
|---|---|---|
| Container (LXC) | Proxmox itself (its root filesystem, in the cluster's resource list) | the root filesystem, always |
| VM | the **QEMU guest agent** inside the VM (`get-fsinfo`) | every real filesystem mounted in it (`/`, `/data`, `C:\`) |

Proxmox cannot look inside a VM's disk, so a VM needs the agent:

1. In the VM: install and start `qemu-guest-agent` (Debian/Ubuntu: `apt install qemu-guest-agent`;
   Windows: the VirtIO guest tools).
2. In Proxmox: VM → *Options* → *QEMU Guest Agent* → enabled, then restart the VM once.
3. The API token needs `VM.GuestAgent.Audit` on the VM (`VM.Monitor` before Proxmox 9) - the
   same privilege IP discovery uses ([service_ips.md](service_ips.md)).

A VM without the agent shows **not measured: no guest agent** and is never alerted. Hover the
text for the reason Proxmox gave. Pseudo and image filesystems (tmpfs, overlay, squashfs snaps,
ISO images) are left out, and a disk mounted at several places (bind mounts, Docker volumes) is
counted once.

## When it alerts

The **fullest** filesystem of a guest decides:

| | at | alert | severity |
|---|---|---|---|
| low | `DISK_WARN_PCT` (85%) | `disk_low`: "Disk on web-vm is 91% full (/)" | warning |
| full | `DISK_FULL_PCT` (95%) | `disk_full` | error |
| fine again | 3 points under the threshold it was at | `recovered` | info |

The alert's text names every filesystem ("/ is 91% full (18.2 GB of 20.0 GB used); /data 40%").
Like every other alert it is debounced (two cycles), flap-damped, grouped by the rules and sent by
default; a rule can pick level **disk** or the events `disk_low` / `disk_full`, and scope it by
node, service or group (a guest that is a service carries its slug and group). Tapping it opens
Nodes & guests filtered to that guest. A guest that is switched off, or cannot be measured, keeps
whatever it was last announced as.

## Seeing it

* **Nodes & guests**: the *Disk* column (fullest filesystem, its size; hover for all of them),
  the *Disk filling up* filter, and *⋯ → Check guest disks now*.
* **API**: `GET /api/infra/disks` (fullest first, `?node=`), `POST /api/infra/disks/check`
  (read now); each row of `GET /api/infra/proxmox/guests` carries its `disk`.
* **Assistants**: `disk_usage` (`refresh=true` reads now) - see [mcp.md](mcp.md).

The last result is kept in the `disks.status` setting, so a page load never waits on Proxmox.
