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
  `sub_start:<slug>` / `sub_gate:<slug>`. Backups of a sub-service on its own are refused.
* Notifications, controllers and recovery work at every level; see
  [notifications.md](notifications.md), [controllers.md](controllers.md) and
  [recovery.md](recovery.md).

## The map (`/map`)

Everything comes from `GET /api/map` (login required), built from the registry and the
last scan - no live calls, so the page is instant. It refreshes itself every 30 seconds.

**Boxes view** - three columns:

| Column | Shows |
|--------|-------|
| Entry | each proxy host Traefik serves (struck through when not served) |
| Boxes | node -> guest (VM/CT, status) -> service cards, sub-services nested inside their parent |
| Backups | the Proxmox Backup Server datastore |

Lines (toggle each one at the top; hover a card to follow only its lines):

| Line | Meaning |
|------|---------|
| blue | traffic: proxy host -> the service that answers it (faint when not served) |
| grey dashed | `depends_on` |
| green | backup: service's guest -> PBS, labelled with the policy (faint = never backed up) |
| amber dotted | the backup waits for this sub-service (`backup_gate: required`) |
| purple dashed | PBS needs this service awake (`backup_requires`) |

Card colour = verdict (green healthy, red failed, blue down, grey disabled/parked/unknown).
Click a card for details and **Start / Stop / Restart** (same audited actions as the
service page). There is also a **Tree** view - the same data as a plain indented list -
plus a search box and an "only problems" filter.
