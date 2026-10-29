# Boot order and start at boot

Proxmox decides what starts when a node boots. Each guest has two settings, and the node has
one:

| Where | Setting | What it does |
|---|---|---|
| guest | **Start at boot** (`onboot`) | Proxmox starts the guest when the node boots. |
| guest | **Start/Shutdown order** (`startup: order=N,up=S,down=S`) | The order guests are started in (lowest first; guests without an order come after the ordered ones, then by VMID), and how many seconds Proxmox waits after starting this one before the next (`up`). |
| node | **Start on boot delay** (`startall-onboot-delay`) | Seconds Proxmox waits after the node is up before it starts the first guest. |

The orchestrator reads these, keeps them, shows them and takes them into account when it
wakes something.

## Reading and keeping them

A node that is off cannot be asked, so the last values read while it was on are kept
(`guest_boot_setting` table, and `onboot_delay_s` / `boot_read_at` on the node's state). They
are read again:

* by **Nodes & guests** (`GET /api/infra/proxmox/guests`), for every online node whose copy is
  older than 10 minutes;
* by the **wake pipeline**, right after it powered a node on (step `boot_settings`).

Reading never fails anything: a guest whose settings cannot be read keeps its last copy.

## Seeing and changing them

**Nodes & guests**: each node card says what Proxmox starts when it boots and how long the node
usually takes to come up. The guests table has a *Starts with node* column: *yes, 2nd* with the
order, the wait and roughly when it starts after boot; *no: started when needed*; or *not read
yet*. A guest's menu has **Boot settings…**, which changes them in Proxmox
(`PUT /api/infra/guests/{node}/{vmid}/boot`, audited as `infra.guest_boot`). The node must be
on.

**A service's page**: the Overview says whether its guest starts with its node (and where in
the order), and how long waking it takes now and from off. The *Backups* tab's backup plan lists
the same per step, with the boot order of every node the backup would power on (the guests it
needs in bold).

## What a wake does with them

After a wake (by a person, by traffic, or for a backup) **powered the node on**:

* A guest with **Start at boot** is left to Proxmox. The wake waits for it to be running, for
  up to its place in the boot order (the node's delay, plus each earlier guest's start and
  `up` wait) plus `WAKE_AUTOSTART_GRACE_S` (default 60 s). Step `guest_autostart`: `ok`, or
  `late` - then it is started anyway, so a guest that Proxmox skipped still comes up.
* Any other guest is started, as before.

When the node was **already on**, the guest is started if it is stopped: Proxmox only starts
guests at boot.

## The estimates

How long a wake takes is estimated from history where there is some, and a guess where there is
none (marked as such):

* the node coming up: the median of its last 5 wakes that powered it on (`node_online` step),
  else 2 minutes;
* its place in the boot order: from the settings above, 5 s per guest start plus each `up`
  wait;
* the service answering: the median of its last 5 health waits (`health_poll`), else 45 s.

Example: `node-b` is off, takes about 2 min to come up, starts `db` (order 1, up 30 s) and then
`app`. Waking `app`: power node-b on and wait (2 min), Proxmox starts it by itself (2nd in the
boot order, ~35 s), wait until it answers (45 s): about 3 min 20 s.
