# Plan: the service tree, controllers, notifications, recovery and the map

Goal: see and act on everything from one place — nodes, guests, services and the
things inside them (Portainer's containers, Home Assistant's add-ons) — with probes,
actions, notifications, backup rules and limited automatic recovery at every level,
and a picture of what runs where and where traffic and backups flow.

## Decisions

1. **A tree, not a rewrite.** `Service` stays the unit. A *sub-service* is a `Service`
   with a `parent`. The root keeps the guest link (backups and power are per guest);
   children inherit the guest and node. Guests are not a table: a guest is the root's
   guest side, and guest-level incidents are keyed by `node/vmid`, so several root
   services on one guest still produce one guest alert. (If that ever needs its own
   settings, promoting Guest to a table is additive.)
2. **One machinery for every level.** Probes, state, actions, notification rules and
   recovery policy all target `node | guest | service | sub`. Nothing is duplicated
   per level.
3. **Fail at the lowest level, suppress above.** A child whose guest or node is down is
   DOWN (not FAILED) and its alert folds into the guest/node alert. Recovery acts on
   the highest failed level only.
4. **Automatic recovery is bounded and opt-in.** Per service: off by default; a ladder
   of steps; a cap per time window; then it stops and escalates. Restarting a guest is
   a separate opt-in. Restores are proposals you approve, never automatic.
5. **`enabled` means "managed and served".** Start/Stop/Restart are explicit actions at
   each level and never a side effect of the toggle.

## Behaviour

| Area | Rule |
|---|---|
| Sub-service | `parent: <slug>`; no guest of its own; `node` copied from the parent; own probes, proxy hosts, controller, recovery, backup setting. YAML stays flat (`parent:` key). |
| Health | Guest gate uses the root's guest. Parent health does not block children (a dead Portainer API says nothing about a container). `depends_on` works as before. A tree rollup (worst of self and required children) is *displayed*, never fed into a verdict. |
| Proxy hosts | Link to any service or sub-service. A host is served only if it, its service and every ancestor are enabled; the host's own switch is never rewritten, so re-enabling restores exactly what was on. The `traefik:` probe follows the effective state. |
| Backups | Policy lives on the root. Each sub-service: `backup_gate` = `required` (must be healthy before the dump; its controller starts it if stopped) or `ignore` (default; a container you stopped on purpose never blocks a backup). A root's gate applies whether or not it is enabled. |
| Controllers | Pluggable like the proxy/power adapters: `status/start/stop/restart`. Portainer (containers), Home Assistant (add-ons/core), docker/systemd over a transport, Proxmox guest, dry-run. Config per service: `controller: {type: portainer, endpoint: 2, container: nginx}`. Every action is audited. |
| Notifications | Channels: ntfy, SMTP, webhook. Rules by level/scope/event/severity/channels. Fired on state *changes* after two consecutive scans, flap-damped, grouped under the root cause, logged in a feed. Events: down, failed, recovered, flapping, recovery attempt/exhausted, restore proposed, node unreachable. |
| Recovery | Policy per service: `enabled`, `steps` (`restart`, `restart_parent`, `restart_guest`, `propose_restore`), `max_attempts`, `window_s`, `settle_s`. State per service; reset on healthy; notifies on each attempt and on exhaustion. Global kill switch `RECOVERY_ENABLED`. |
| Map | `/map`: nodes as boxes holding their services, sub-services nested; entry column (Traefik + proxy hosts) with edges to what they serve; dependency edges; backup flow to the PBS datastore and policy prerequisites. Health colours, guest/power badges, click for actions. Auto-refresh. |

## Build order (one commit each, tests with each)

1. **Tree model**: `service.parent_id`, YAML/loader/API/CLI, dashboard nesting, guest
   inheritance in health/wake, proxy-host cascade, sub-service create/edit.
2. **Notifications**: real channels, rules, state-change engine with root-cause
   grouping and flap damping, node monitoring, feed page `/notifications`.
3. **Controllers + actions**: interface, Portainer, Home Assistant, docker/systemd,
   Proxmox guest, dry-run; Start/Stop/Restart on service/sub/guest/node in the UI,
   API and CLI.
4. **Recovery**: policy, ladder, limits, state, notifications, restore proposals.
5. **Backup rules**: `backup_gate` for sub-services in the gate and the wake step.
6. **Map**: `/api/map` + the page.
7. Docs and the full suite.

## Defaults I chose where it was open

* A guest shared by several root services is allowed; guest alerts are per guest.
* Automatic guest restarts: never unless a service lists `restart_guest`.
* Notification channels come from the environment (ntfy / SMTP / webhook); rules and
  the feed live in the database.
* No new JS libraries: the map is plain SVG + HTML like the rest of the UI.
