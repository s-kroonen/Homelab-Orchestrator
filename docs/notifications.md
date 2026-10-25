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

Every health scan the engine looks at nodes (Proxmox cluster status), guests (running
or not) and every service and sub-service, and announces CHANGES:

* **failed** (a required probe failed), **down** (the node or guest is not running),
  **recovered** - and only a recovery for something that was announced.
* Debounced: a change must be seen on `NOTIFY_CONFIRM_SCANS` (2) consecutive scans.
* Grouped under the root cause: a service that is down because its node or guest is
  down is *folded into* that alert (kept in the feed, marked "covered by node:pve1"),
  so a dead host sends one alert, not one per container.
* Flap-damped: `NOTIFY_FLAP_THRESHOLD` (4) changes inside `NOTIFY_FLAP_WINDOW_S`
  (30 min) = one "flapping" alert, then quiet until it settles.
* UNKNOWN ("we could not look") is never announced.

Pipelines add **backup_failed** and **wake_failed**; the recovery loop adds
**recovery_attempt / recovery_succeeded / recovery_exhausted** and **restore_proposed**. Stacks
linked to Portainer add **switch_failed**, **outside_change** (a linked thing was changed outside
the orchestrator), and for deploys **deploy_blocked**, **deploy_failed**, **deploy_succeeded**
and **deploy_outside** - see [portainer_deploys.md](portainer_deploys.md). A dangerous request from an
AI assistant adds **mcp_request** (with the approval link) - see [mcp.md](mcp.md).

## Rules

With **no rules**, `failed`, `down`, `recovered`, `flapping`, `backup_failed`, `wake_failed`,
`recovery_succeeded`, `recovery_exhausted`, `restore_proposed`, `switch_failed`, `outside_change`,
`deploy_blocked`, `deploy_failed` and `mcp_request` go to every channel
(`recovery_attempt` stays in the feed unless a rule asks for it). A rule narrows or routes: levels
(`node` / `guest` / `service` / `sub`), scope (services, groups, nodes), events, the
least severity (`info` < `warn` < `error` < `critical`) and channels. An empty list
means "any". Once any rule exists, an event is sent only if some enabled rule matches;
the rest stay in the feed marked "no rule wants this". Rules live in the database
(not `services.yaml`) and are part of the System page's backups.

## The feed

Everything the engine decided, sent or not, kept `NOTIFY_RETENTION_DAYS` (30) days.
`orchestrator-cli notify feed` prints it; `notify rules` lists the rules.
