# Bulk actions

Tick rows, pick an action, get a per-item report. Available on:

| Page | Rows | Actions |
| --- | --- | --- |
| Dashboard, services tables | services | Enable, Disable, Wake, Run backup now, Probes required, Probes optional, Set group, Set policy |
| Dashboard, Nodes | nodes | Wake, Power off (with the *force* checkbox), Wake on hit on / off |
| Proxy hosts | proxy hosts | Enable, Disable, Delete, Wake on hit on / off |
| Guests | Proxmox guests | Start, Stop |

## Using it

* A checkbox column comes first in each table. The header checkbox selects every
  **visible** row of that table - rows hidden by the page's filter box are left
  alone - and shows a partial state when only some are ticked.
* As soon as something is selected, an action bar appears above the table with
  "N selected", the actions and **Clear**. It wraps on a phone.
* Destructive actions (Disable, Delete, Power off, Stop, Run backup now) ask
  first, listing the items.
* Results are always per item: a toast with the counts (and the first failure),
  plus a list under the bar - one line per item, ✓ with what happened or ✗ with
  the reason. Failed items stay ticked, so retrying is one click. Pages that
  reload to show the change (the dashboard) keep the last report across the
  reload; **Dismiss** removes it.

Nothing is all-or-nothing: each item runs on its own, one after another, and one
failure never undoes or blocks the rest.

## API

```
POST /api/bulk/services     {"slugs": [...], "action": ..., "params": {...}}
POST /api/bulk/proxy-hosts  {"ids": [1, 2], "action": ...}
POST /api/bulk/nodes        {"names": [...], "action": ..., "params": {"force": false}}
```

`ids`, `slugs` and `names` are interchangeable. Actions: services `enable`,
`disable`, `wake`, `backup` (synchronous, like `POST /api/backup/{slug}`),
`probes_required`, `probes_optional`, `update` with `params` `group` and/or
`backup_policy_id`; proxy hosts `enable`, `disable`, `delete`, `wake_on_hit_on`,
`wake_on_hit_off`; nodes `wake`, `power_off`, `wake_on_hit_on`, `wake_on_hit_off`.
At most 200 items per request. Response:

```json
{"resource": "services", "action": "disable", "params": {},
 "results": [{"id": "media", "ok": true, "message": "disabled"},
             {"id": "nope", "ok": false, "message": "no service registered with slug 'nope'"}],
 "ok_count": 1, "failed_count": 1}
```

An unknown action, an empty selection or bad `params` is a `400` for the whole
request; anything about one item (not found, validation, Traefik Manager or
Proxmox refusing) is that item's `ok: false`.

Guests have no bulk endpoint: the page calls, per guest and two at a time,
`POST /api/services/{slug}/guest/power` for a guest registered as a service (its
"never stop mid-backup" guardrail applies) and the new
`POST /api/infra/guests/{node}/{vmid}/power` `{"action": "start"|"stop"}` for
any other guest (graceful shutdown; audited as `infra.guest_power_action`; it
hands a registered guest to the service path as well).

## Same code path, same audit

Every bulk action calls the same function as its single-item endpoint (the
handlers were split into callables: `apply_service_update`,
`apply_probe_update`, `apply_node_update`, `apply_node_power`, `start_wake`,
`run_backup`, `set_proxy_host_enabled`, `delete_proxy_host_record`,
`guest_power_for_service`), so validation, live write-through (Traefik Manager
before the database) and audit entries are identical: one entry per item with
the single-item action name (`service.updated`, `probe.updated`,
`proxy_host.disabled`, `proxy_host.deleted`, `node.power_action`, ...), plus one
`bulk.services` / `bulk.proxy_hosts` / `bulk.nodes` summary per request with the
action, params, the ids that succeeded and the ones that failed with why.

## CLI

```
orchestrator-cli bulk services enable|disable|wake|probes-required|probes-optional \
    (--all | --group NAME | --slug a,b) [--dry-run] [--file services.yaml]
orchestrator-cli bulk proxy-hosts enable|disable \
    (--all | --service SLUG | --hostname a.example.com,b.example.com) [--dry-run]
```

One line per item (`ok` / `FAILED: why`), exit code 1 if any item failed,
`--dry-run` only prints the selection. Services `enable`/`disable`/`probes-*`
edit `services.yaml` like `service update`; `wake` runs the wake pipeline per
service like `wake SLUG`. Proxy hosts go through the same function as the web
(Traefik Manager first, then the database, audited with actor `cli`) and the new
flag is mirrored into `services.yaml` so a later YAML reload agrees. `DRY_RUN`
is honoured: with it on, no power/Proxmox/Traefik Manager call is made.
