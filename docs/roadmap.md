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
   a schedule. ~~Each needs one edit+save to trigger it once, or a small
   one-off backfill~~ — **done**: `orchestrator-cli proxy resync-maintenance`
   (see Small features below).

## Small features (each independently schedulable, no architecture decisions needed)

- ~~**Add a proxy host directly from the service page.**~~ **Done**, now
  superseded. An inline "Add proxy host" form used to live on
  `service_detail.html` itself; it's been replaced by a link to the
  dedicated `/proxy-hosts/new?service_id=<id>` page (see the next item) —
  "the same edit on the same page everywhere" was explicitly the pattern
  being fixed, so a second near-duplicate copy of the form was removed
  rather than kept. This service's own proxy hosts list (read-only, now
  with an **Edit** link alongside **Delete**) stays. See
  [proxy_hosts.md](proxy_hosts.md#where-to-edit-them).
- ~~**Proxy hosts: live-managed route CRUD + dedicated add/edit pages.**~~
  **Done.** Creating/editing/deleting a `ProxyHost` now creates/edits/
  deletes the real route on Traefik Manager (when `router_provider` is
  `traefik` and an `upstream` is given) — not just a local reference
  record separately reconciled via Resync. New `ProxyAdapter.create_route`/
  `update_route`/`delete_route` (`orchestrator.adapters.proxy.base`,
  implemented in `TraefikManagerProxyAdapter` and faked in
  `DryRunProxyAdapter`), write-through wired into
  `web/routers/proxy_hosts.py`'s create/update/delete endpoints, with the
  backend route reference persisted in
  `ProxyHost.extra["traefik_manager_route_id"]`. `/proxy-hosts` is now
  list-only (plus a filter box); `/proxy-hosts/new` and
  `/proxy-hosts/{id}/edit` are real, dedicated pages — the first page to
  get this treatment (more follow later; this was scoped to proxy hosts
  only). Resync's role shifts from "the only way to reach parity" to the
  bulk-discovery/drift-catch-up path. See
  [proxy_hosts.md](proxy_hosts.md#live-managed-proxy-hosts) for the full
  write-through/rollback/backfill behaviour.
- ~~**Backfill command for the maintenance middleware**~~ **Done.**
  `orchestrator-cli proxy resync-maintenance` re-runs
  `sync_maintenance_middleware` for every `ProxyHost` with a linked
  `service_id`, for use after `ORCHESTRATOR_PUBLIC_URL` is set for the
  first time or after a Traefik Manager migration. See
  [proxy_hosts.md](proxy_hosts.md#backfilling-existing-proxy-hosts).
- ~~**Backups-per-service view.**~~ **Done.** A "Backups" section on
  `service_detail.html` lists this service's recent `BackupRecord`s
  (created, verified, protected, size, run), from the existing
  `GET /api/backups?slug=` — no new endpoint needed, the filter already
  existed.
- ~~**Backup-policy logs/history view.**~~ **Done.** The existing
  `/backup-policies` page gets a per-policy, lazily-loaded "History"
  expander, backed by a new `?policy_id=` filter on `GET /api/runs`
  (joined through `Service.backup_policy_id` — the same single source of
  truth every other policy-membership query already uses, chosen over a
  dedicated `BackupRecord`-based endpoint because `BackupRecord` only
  covers backups that got as far as writing one, and a failed run is
  exactly what this view most needs to surface).

## Done: shared template/CSS/JS foundation

~~**Every page was a fully standalone HTML document** (own `<html><head>`,
own duplicated `<nav>`, own duplicated dark-theme `<style>` block, no shared
JS) — nothing new built on a foundation, every feature reinvented its own
chrome.~~ **Done.** `base.html` (Jinja2 `{% block %}` inheritance — every
other template now extends it), `static/app.css` (the near-identical
per-page `<style>` blocks consolidated into one stylesheet) and
`static/app.js` (`toast()` + an `apiFetch()` wrapper that auto-shows a toast
for any non-2xx response or network failure, replacing the bespoke
`sendJson`/`errorText`/`apiError` helpers copy-pasted into several templates)
now carry the chrome and the error-surfacing every page shares. This was
scoped as foundation only — chrome + error handling, no change to any page's
actual information architecture — so the deeper redesign (sub-tabs,
dedicated add/edit pages, list filtering) mentioned below was still open at
the time. **Proxy hosts got dedicated add/edit pages + list filtering
first** (see the "Small features" entry above) — the other list pages
(backup policies, services) still have the bolted-on-inline-form pattern
and are candidates for the same treatment next. See "The big question:
frontend direction" below for why this stays server-rendered Jinja2 rather
than a SPA rewrite.

## Architecture: generalize the adapter pattern

This app already has the right shape for *some* integrations —
`orchestrator.adapters.{pbs,power,proxmox}` are each an ABC with a `dry_run`
implementation and one or more real ones (`MqttPowerAdapter` today,
e.g. a future `HttpPowerAdapter` sitting right alongside it with no interface
change needed). ~~The gap: **proxy management doesn't follow this pattern
yet** — `TraefikManagerClient` (`registry/traefik_manager_client.py`) was
built as a standalone client, not as a `ProxyAdapter` ABC implementation,
because at the time there was only one real backend to support.~~

**Done** (proxy adapter): new `orchestrator.adapters.proxy` package —
`ProxyAdapter` ABC (`list_routes`, `ensure_maintenance_fallback`,
`remove_maintenance_fallback`, and (added later, for live-managed proxy
hosts — see [proxy_hosts.md](proxy_hosts.md#live-managed-proxy-hosts))
`create_route`/`update_route`/`delete_route` — domain verbs, shaped from
what the maintenance-fallback feature, drift detection and live route CRUD
actually need, not a passthrough of Traefik-Manager-specific REST shapes),
`DryRunProxyAdapter`,
and `TraefikManagerProxyAdapter`, which is where the old
`registry/traefik_manager_client.py` + `traefik_manager_sync.py` logic
actually lives now (both deleted — no parallel implementation was kept).
`web/routers/proxy_hosts.py` and `orchestrator-cli proxy resync-maintenance`
both go through `adapters.proxy` (`AdaptersDep` / `build_adapters()`)
instead of constructing a client directly. New `PROXY_ADAPTER` setting,
same `dry_run`/live shape as the other three, with the factory falling back
to `dry_run` (loudly) when `TRAEFIK_MANAGER_URL` is blank — see
`docs/architecture.md`. Not urgent to add a second backend (bare Traefik,
NPM, Pangolin) — the point was that the seam now exists for one to be
additive later, not a rewrite.
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
