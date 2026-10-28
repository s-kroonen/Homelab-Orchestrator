# Sub-services, the tree and the map

A **service** is one thing you run and probe. A **sub-service** is a service that lives
*inside* another one - the containers on a Portainer VM, the add-ons on Home Assistant.
Both are the same kind of row; a sub-service just has a `parent`.

```yaml
services:
  - slug: portainer
    name: Portainer
    node: pve1
    guest_kind: vm
    guest_id: 101
    backup_policy: daily
  - slug: nginx
    name: nginx container
    parent: portainer            # lives on portainer's guest and node
    backup_gate: required        # wait for it during portainer's backup
    controller: {type: portainer, endpoint: 2, container: nginx}
    recovery: {enabled: true, max_attempts: 2}
    proxy_hosts:
      - {hostname: nginx.example.com, upstream: "http://10.0.0.5:8080"}
```

## Rules of the tree

* The **root** owns the guest, the node and the backup policy. A sub-service inherits the
  guest and node and cannot have its own guest or backup policy. Depth is at most 4.
* **Disabling a service parks its whole subtree**: its proxy hosts stop being served
  (the host's own switch is AND-ed with every ancestor's), the scanner skips it and the
  map shows it as *parked*. The guest keeps running. This is how you switch off one
  container's proxy hosts without touching the VM.
* **Health**: a sub-service is checked through its own probes. If the guest or node is
  down it is DOWN (not FAILED) and its alert folds into the guest/node alert. A failing
  parent does not by itself fail its children.
* **Backups** run per guest, on the root. Each sub-service has a `backup_gate`:
  `ignore` (default) or `required` - the backup starts the sub-service through its
  controller if it is stopped, waits until it is healthy, and the plan lists it as
  `sub_start:<slug>` / `sub_gate:<slug>`. Backups of a sub-service on its own are refused (a database can be *dumped* on its own instead: [dumps.md](dumps.md)).
* Notifications, controllers and recovery work at every level; see
  [notifications.md](notifications.md), [controllers.md](controllers.md) and
  [recovery.md](recovery.md).

## The map (`/map`)

Built to the Map wireframe ([docs/design](design/README.md)). The page paints at once from the
database (`GET /api/views/map`: `GET /api/map`'s picture plus each service's row as on the
Services page, the nodes and the policies' next runs) and then reads what is live: node power
and Proxmox versions (`/api/infra/nodes/status`) and how full the datastore is
(`/api/infra/pbs/datastore`). It refreshes in place on live events.

**One row per node**, in three columns:

| Column | Shows |
|--------|-------|
| Way in | the proxy hosts that lead to the node's services, grouped by service (faint and "off" when not served) |
| Node | the node (power, wakes on demand or always on, address, Proxmox version, Wake or Check) and one row per service: VM/CT id, name, state |
| Backups | one card for the backup server every node shares: how full it is, then per node how many guests it backs up, the policy and its next run, and whether it wakes the node and puts it back off |

Services without a node, and proxy hosts that lead to no service, get a "No node" row at the end.

**Click** a node, service, proxy host or the backup server to select it. The selection sidebar
says what it is and its state, why (for example "Not broken: its node is off. Wake it and it
comes back."), the actions (Wake, Check now, Back up now, Open, Edit, and the rest in ⋯), its
details, its proxy hosts, **the path** (traffic through Traefik, where it runs, what must be
awake, where it backs up to), its backups and its recent activity. The selection is in the URL
(`?sel=svc:<slug>`), so a link opens the map with it selected. On a narrow screen the sidebar
slides over the page.

**Lines** are drawn for what is selected (or for everything, with **All**):

| Line | Meaning |
|------|---------|
| blue | traffic: proxy host -> the service that answers it (faint when not served) |
| grey dashed | `depends_on` |
| green | backup: service -> its node's row on the backup server (faint = never backed up) |
| amber dotted | the backup waits for this sub-service (`backup_gate: required`) |
| purple dashed | the backup needs this service awake (`backup_requires`, a policy's `requires`) |

Sub-services sit inside their parent's row (`+2`); their lines start there. **Find** filters on
node, service, host, address and group; **Problems only** keeps failed and warning services.
