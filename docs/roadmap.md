# Roadmap / backlog

A living list of what's next, captured so it survives between sessions. Items
are grouped by size, not by when they'll happen — pick off whatever's next
from each group as it comes up. See each linked doc for the detail on
anything already built; this file is for what *isn't* yet.

## Quick fix (operational, not code)

**Maintenance page / wake isn't firing for a linked proxy host.** Not a bug —
`ORCHESTRATOR_PUBLIC_URL` is unset, and `sync_maintenance_middleware`
(`docs/proxy_hosts.md`) is a deliberate no-op without it, same convention as
every other optional integration here. Once it's set:

1. Set `ORCHESTRATOR_PUBLIC_URL` to this orchestrator's address **as reachable
   from the gateway VM** (e.g. `http://10.0.0.5:8000`).
2. New `ProxyHost`s get the maintenance middleware injected automatically on
   create.
3. **Existing** `ProxyHost`s created before this was set do NOT get it
   retroactively — the sync only runs on create/update/delete/re-link, not on
   a schedule. Each needs one edit+save to trigger it once, or a small
   one-off backfill (loop every `ProxyHost`, call `sync_maintenance_middleware`
   with its current `(hostname, service_slug)` as both old and new). Worth
   adding as a tiny CLI command (`proxy resync-maintenance` or similar) rather
   than a manual per-host edit — see Small features below.

## Small features (each independently schedulable, no architecture decisions needed)

- **Add a proxy host directly from the service page.** Today `ProxyHost`
  management only lives on `/proxy-hosts`; `service_detail.html` just links
  out to it. Add an inline "Add proxy host" form on the service page itself
  (hostname, upstream, router_provider), calling the existing
  `POST /api/proxy-hosts` with `service_id` pre-filled — no new endpoint,
  just UI.
- **Backfill command for the maintenance middleware** (see Quick fix above) —
  a CLI command that re-runs `sync_maintenance_middleware` for every existing
  `ProxyHost`, for use after `ORCHESTRATOR_PUBLIC_URL` is set for the first
  time or after a Traefik Manager migration.
- **Backups-per-service view.** The data already exists (`GET /api/runs`,
  `GET /api/runs/{id}`, `GET /api/backups` in `web/routers/backup.py`) but
  nothing on the service page surfaces it. Add a "Backups" section to
  `service_detail.html`: recent runs for this service (status, started/
  finished, error if failed), filtered from the existing endpoints (add a
  `?service=` filter if they don't already support one — check first).
- **Backup-policy logs/history view.** Similarly: a policy's own run history
  across every service it covers, not just one service's. Likely a new page
  (`/backup-policies/{id}`) or a section on the existing policies page —
  check `docs/` for whatever that page already is before adding a new one.

## Architecture: generalize the adapter pattern

This app already has the right shape for *some* integrations —
`orchestrator.adapters.{pbs,power,proxmox}` are each an ABC with a `dry_run`
implementation and one or more real ones (`MqttPowerAdapter` today,
e.g. a future `HttpPowerAdapter` sitting right alongside it with no interface
change needed). The gap: **proxy management doesn't follow this pattern yet**
— `TraefikManagerClient` (`registry/traefik_manager_client.py`) was built as
a standalone client, not as a `ProxyAdapter` ABC implementation, because at
the time there was only one real backend to support.

Now that the actual need is named (room for Traefik Manager today, bare
Traefik, NPM, Pangolin, or something else later, without rewriting every
caller), formalize it:

- New `orchestrator.adapters.proxy` package: `ProxyAdapter` ABC (methods like
  `list_routes`, `inject_middleware`, `remove_middleware` — shaped from what
  `traefik_manager_sync.py` actually needs today, not speculative extras),
  `DryRunProxyAdapter`, and `TraefikManagerProxyAdapter` wrapping the existing
  client. Everything in `traefik_manager_sync.py` and the proxy-hosts router
  that currently imports `TraefikManagerClient` directly switches to the
  adapter interface instead — mechanical, low-risk, same diff shape as any
  other adapter.
- **Not urgent to add more backends now** — the point is the seam exists so a
  second one (bare Traefik file-provider writes, NPM, Pangolin) is additive
  later, not a rewrite. Don't build a second proxy backend speculatively;
  build the ABC now, add implementations when a real one is needed.
- Power: no structural change needed — `PowerAdapter` already supports this.
  When the direct-HTTP power manager exists, it's a new `HttpPowerAdapter`
  class next to `MqttPowerAdapter`, selected the same way the factory already
  picks between `dry_run`/`mqtt` today. Flagged here only so it isn't
  forgotten, not because it needs design work now.

## The big question: frontend direction

**My recommendation: don't do a full rewrite. Add PWA capability (manifest +
service worker + Web Push) and live updates (SSE or a WebSocket) incrementally
on top of the existing server-rendered Python/Jinja2 app, not a SPA rewrite.**

Why: a PWA (installable, works offline-ish, gets push notifications) and a
single-page-app framework are two different axes, not one choice — a
manifest.json + a small service worker + the Push API work fine bolted onto
server-rendered pages, no bundler or component framework required. "Easier
UI updates" (live state instead of the current `setInterval(reload)` pattern)
is the same story: Server-Sent Events or a plain WebSocket channel gets you
real push-based UI updates without adopting React/Vue/Svelte. This keeps
faith with a discipline this app has followed in every PR so far (explicitly:
"no new build tooling / no frontend framework").

A full rewrite's real cost: every existing page gets re-built before any new
feature ships, the API surface this session has spent a long time getting
right (health scanning, Traefik Manager integration, auth, proxy hosts) needs
a second client written against it, and the CLI-as-fallback property survives
either way since the CLI already talks to the DB/pipelines directly, not
through whatever serves the browser — so the rewrite doesn't actually buy
that property, it's already true today.

**If the operator still wants a real SPA/dedicated frontend app after seeing
what the incremental PWA path can't do, that's a legitimate later call** —
but it should be made after hitting a concrete wall, not ahead of one.

### Suggested sequencing (pending confirmation)

1. The quick fix + small features above — all independent, all needed now,
   none blocked by the frontend question.
2. The adapter-pattern generalization — independent, low-risk, worth doing
   while the Traefik Manager integration is fresh.
3. PWA + live-updates (manifest, service worker, Web Push, SSE/WebSocket) on
   the existing frontend — only large item on this list, scoped deliberately
   to avoid a rewrite.
4. Re-evaluate a dedicated SPA/rewrite only if (3) proves insufficient for a
   concrete, named reason.
