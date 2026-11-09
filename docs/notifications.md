# Notifications

Alerts for every level - nodes, guests, services and sub-services - plus backup,
wake and recovery events, delivered to ntfy, e-mail and/or a webhook, with a feed
and rules on **/notifications**.

## Channels

Configured in `.env` (see `.env.example`); several can be active at once.

| Channel | Needs |
|---|---|
| `ntfy` | `NTFY_URL` (the topic URL), optional `NTFY_TOKEN` |
| `email` | `MAIL_SMTP_HOST`, `MAIL_SMTP_PORT`, `MAIL_FROM`, `MAIL_TO` (comma-separated), optional `MAIL_SMTP_USERNAME` / `MAIL_SMTP_PASSWORD`, `MAIL_SMTP_STARTTLS` |
| `webhook` | `NOTIFY_WEBHOOK_URL` (JSON POST: `severity`, `title`, `body`, `tags`, `approve_url`), optional `NOTIFY_WEBHOOK_TOKEN` |

`NOTIFIER_ADAPTER=auto` uses every channel that is configured (`ntfy`, `email` or
`webhook` restricts it to one; `null` logs only). **`DRY_RUN=true` keeps the notifier
silent whatever this says.** Check it: the **Send a test notification** button, or
`orchestrator-cli notify test`, shows what each channel did - success or the error.

## What triggers an alert

Every health scan the engine looks at the orchestrator's own network (its LAN router, the
internet, the proxy - System → Network), nodes (Proxmox cluster status, or whether the node
answers when no API does), guests (running or not), every service and sub-service, and every
proxy host's route (inside through the proxy, and from outside), and announces CHANGES:

* **failed** (a required probe failed), **down** (the node or guest is not running, or it is
  not reachable while the orchestrator's own network is fine), **unknown** (it cannot be
  determined - rare by design, see [architecture.md](architecture.md#detection)),
  **recovered** - and only a recovery for something that was announced.
* Severity: a node, the orchestrator's network or a route going down is an **error**, as is a
  service that is unreachable; a service down because its guest was switched off is a
  warning, unknown is a warning.
* Debounced: a change must be seen on `NOTIFY_CONFIRM_SCANS` (2) consecutive scans, unless a
  rule with **Send on the first check** matches it: then it goes out on the first scan.
* Grouped under the root cause: a service that is down because its node or guest is
  down is *folded into* that alert (kept in the feed, marked "covered by node:pve1"),
  so a dead host sends one alert, not one per container.
* Flap-damped: `NOTIFY_FLAP_THRESHOLD` (4) changes inside `NOTIFY_FLAP_WINDOW_S`
  (30 min) = one "flapping" alert, then quiet until it settles.
* Effects of the orchestrator's own network being down are folded under its LAN router
  check (they cannot be judged, and nothing could be sent anyway until it is back).

Guests' disks add **disk_low** (a filesystem at `DISK_WARN_PCT`, 85%: a warning) and **disk_full**
(`DISK_FULL_PCT`, 95%: an error), sent by default, with *recovered* when there is room again -
see [disks.md](disks.md).

Pipelines add **backup_failed** and **wake_failed** (default), **backup_retry** (default) and
**backup_skipped** (a backup that was not taken because the service was unhealthy, had no
required check or was off and may not be woken - see [backup_wake.md](backup_wake.md); in
the feed, sent only when a rule asks for it); the recovery loop adds
**recovery_attempt / recovery_succeeded / recovery_exhausted** and **restore_proposed**. Stacks
linked to Portainer add **switch_failed**, **outside_change** (a linked thing was changed outside
the orchestrator), and for deploys **deploy_blocked**, **deploy_failed**, **deploy_succeeded**
and **deploy_outside** - see [portainer_deploys.md](portainer_deploys.md). A database dump adds **dump_failed** (default) and **dump_succeeded** - see
[dumps.md](dumps.md). A dangerous request from an
AI assistant adds **mcp_request** (with the approval link) - see [mcp.md](mcp.md).

## Rules

With **no rules**, `failed`, `down`, `unknown`, `recovered`, `flapping`, `disk_low`, `disk_full`, `backup_failed`, `wake_failed`,
`recovery_succeeded`, `recovery_exhausted`, `restore_proposed`, `switch_failed`, `outside_change`,
`deploy_blocked`, `deploy_failed`, `dump_failed` and `mcp_request` go to every channel
(`recovery_attempt` stays in the feed unless a rule asks for it). A rule narrows or routes: levels
(`network` / `node` / `guest` / `service` / `sub` / `route` / `disk`), scope (services, groups, nodes),
events, the least severity (`info` < `warn` < `error` < `critical`), channels, and whether it
sends on the first check. An empty list
means "any". Once any rule exists, an event is sent only if some enabled rule matches;
the rest stay in the feed marked "no rule wants this". Rules live in the database
(not `services.yaml`) and are part of the orchestrator's own backups (Backups → Orchestrator).

## The feed

Everything the engine decided, sent or not, kept `NOTIFY_RETENTION_DAYS` (30) days.
`orchestrator-cli notify feed` prints it; `notify rules` lists the rules.

## The page

Settings → Notifications (`/notifications`) has, on the left, the **Rules** (+ Add rule and Edit
open a drawer; the switch turns a rule on or off; ⋯ has the rest) and **Recent alerts**, the
newest twelve, with what was sent where ("Show folded and unsent" hides what was folded into
another alert or that no rule wanted; Activity → Alerts has the whole feed). On the right,
**Channels** says which of ntfy, e-mail, webhook and push are set up (each is set up or edited
right there, in its drawer), and **This device** turns alerts on or off for the browser you are using,
installs the dashboard as an app, sends a test, and lists every device with push.
**Send a test** in the header sends one to every channel.

With no rule at all, the built-in default is shown as a rule of its own (the events above, every
channel). **Make editable** copies it into a real rule, which then replaces the default.

Each device has its own settings (**Edit** on its row, `PATCH /api/push/subscriptions/{id}`):
its name, whether it gets alerts (a paused device gets only its own tests) and the least severe
alert it gets (everything, warnings and worse, errors and worse, critical only). They apply on top
of the rules: a rule decides whether an alert goes to push at all, the device whether it wants it.

## Where an alert takes you

Every alert opens the thing it is about, from every channel: tapping a push
notification, the ntfy "Open" action, the link in an e-mail, the `approve_url` field of
the webhook, and the target in the Notifications feed and on the Activity page.

| Alert about | Opens |
| --- | --- |
| service / sub-service health (failed, down, recovered, flapping, recovery) | `/services/<slug>#health` |
| backup or restore | `/services/<slug>#backups` |
| deploy | `/services/<slug>#deploys` |
| database dump | `/services/<slug>#dumps` |
| switch failed, outside change | `/services/<slug>#stack` |
| node | `/sources/<proxmox>?tab=nodes` (its Proxmox source, the Nodes tab) |
| guest, or a guest's disk | `/sources/<proxmox>?tab=guests&q=<vmid>` (its Proxmox source's guests, filtered to that guest) |
| self-update | `/system` |
| a request waiting for approval | its approval page (always wins over the above) |

The address comes from `EXTERNAL_URL`, falling back to the passkey origin (`WEBAUTHN_ORIGIN`, the
address you sign in on) and only then to `ORCHESTRATOR_PUBLIC_URL`, which is the address Traefik
forwards to and often a LAN one a phone away from home cannot open. Set `EXTERNAL_URL` to the https
address you use on your phone. Deploy webhook URLs and restore approval links use the same order.

If the service in an old alert has since been deleted, the link lands on a page that says so and
offers to restore it from the [archive](archive.md).

Signed out when you tap an alert? You are sent to the login page and, after the passkey, back to the
same page *and* spot (`?node=…&vmid=…` and `#deploys` are both kept).
