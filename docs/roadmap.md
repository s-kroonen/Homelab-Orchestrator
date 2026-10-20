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
- ~~**Proxy hosts: explicit entry points + verified writes.**~~ **Done.** A
  first live-managed host showed up in Traefik Manager as broken
  (`entryPoint "https" doesn't exist`): `/save` was posted with an empty
  `entryPoints`, which Traefik Manager turns into its default `https`, and every
  edit reset a route the same way. Each proxy host now stores `entry_points` and
  `cert_resolver` (migration `0011_proxy_host_entrypoints`, YAML fields, API),
  chosen from the live Traefik's entry points (`GET /api/proxy/entrypoints`,
  `orchestrator-cli proxy entrypoints`, `--entry-point` / `--cert-resolver`) and
  always sent explicitly. Everything is validated before it is written (hostname,
  upstream, entry points, resolver, conflict with a router that is not ours), and
  after `/save` the runtime router is polled: a rejected create is rolled back, a
  rejected edit returns Traefik's message, and the list shows each host's router
  status in red with Traefik's error. See
  [proxy_hosts.md](proxy_hosts.md#entry-points-and-certificate-resolver).
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
- ~~**Backup policies: dedicated add/edit pages.**~~ **Done.** Same split as
  proxy hosts: `/backup-policies` is now list-only (table + the History
  expanders above + a name filter box); `/backup-policies/new` and
  `/backup-policies/{name}/edit` are real, dedicated pages
  (`backup_policy_form.html`) replacing the bottom-of-page inline form —
  keyed by `name` rather than an id, since `BackupPolicy` has no id exposed
  in the URL elsewhere. UI-only: reuses the existing
  `POST`/`PUT /api/backup-policies[/{name}]`, no backend change. The
  "select group" bulk-target buttons moved onto the form pages intact.
- ~~**Manual backup trigger button.**~~ **Done**, UI-only. `service_detail.html`'s
  Backups section gets a "Run backup now" button calling the existing
  `POST /api/backup/{slug}?verify=true`, disabled for the duration of the
  request (no confirm — an on-demand backup isn't destructive).
- ~~**Manual node power actions.**~~ **Done.** New
  `POST /api/infra/nodes/{name}/power` (`{"action": "wake"|"power_off"|
  "restart", "force": bool}`) calls `adapters.power` directly — NOT the wake
  pipeline — and audit-logs `node.power_action`. `force` maps straight
  through to `PowerAdapter`'s own semantics (false = graceful, true =
  immediate/power-cycle). Wake/Power off/Restart buttons + a shared force
  checkbox added to the dashboard's existing Nodes table; power off/restart
  are gated behind a `confirm()` spelling out what `force=true` means, wake
  is unconditional.
- ~~**Manual service guest start/stop.**~~ **Done.** New
  `POST /api/services/{slug}/guest/power` (`{"action": "start"|"stop"}`)
  calls `adapters.proxmox.start_guest`/`stop_guest` directly — deliberately
  NOT `POST /wake/{slug}` (no node power-on, no hold, no health wait).
  `stop` is refused (`409`) while a backup `PipelineRun` is currently
  `RUNNING` for that service. Audit-logged as `service.guest_power_action`.
  Start/Stop guest buttons added to `service_detail.html` near the service's
  node/guest fields, hidden when the service has no guest configured; stop
  is gated behind a `confirm()`.
- ~~**Left sidebar nav + dashboard pages for the three "no UI" infra
  endpoints.**~~ **Done.** The flat five-link top `<nav>` in `base.html`
  became a grouped left sidebar — **Overview** (Dashboard), **Networking**
  (Proxy hosts, Traffic), **Backups** (Backup policies), **Monitoring**
  (Activity, Connectivity, Guests, Snapshots) — same `request.url.path`
  "current"-highlighting as before, collapsing back to a flat, wrapping top
  bar under 40rem (see `static/app.css`). Pure chrome: no page's own content
  moved, only its nav entry. Bundled into the same change: `GET
  /api/infra/status`/`/api/infra/proxmox/guests`/`/api/infra/pbs/snapshots`
  had working endpoints (`web/routers/infra.py`) but no dashboard page since
  the day they were added — three new list-only pages (`/infra`,
  `/infra/guests`, `/infra/snapshots`; `infra_status.html`/
  `infra_guests.html`/`infra_snapshots.html`) are the dashboard equivalents
  of `orchestrator-cli check`/`guests`/`snapshots`, client-rendered via the
  existing `apiFetch` against those same endpoints — no new API.

- ~~**GeoIP traffic dashboard page.**~~ **Done.** `/traffic/geo`
  (`traffic_geo.html`), server-rendered over `build_geo_view` — the
  aggregation `GET /api/traffic/geo` now shares — with a summary line
  (hits, samples, countries, local vs external share), a countries table
  (flag emoji + CSS percentage bar, LAN traffic as "Local network"), the top
  client IPs and a `?slug=` service filter; a disabled-state notice when
  `GEOIP_ENABLED` is off. Linked to and from `/traffic`. No map — tables
  only, no charting library. The sidebar entry ("Traffic by country") lands
  with the `base.html` restyle.

- ~~**Traffic pages empty when Traefik Manager is the source of truth.**~~
  **Done.** With `TRAEFIK_API_URL` cleared, `/traffic` (Prometheus via
  Traefik's `/metrics`, which Traefik Manager does not proxy) and
  `/traffic/geo` (router -> host lookup via the Traefik API) both stayed
  silently empty. Now one `AccessLogCollector` tails the access log through
  Traefik Manager and feeds per-minute `traffic_rollup` rows (`/traffic`)
  and GeoIP samples (`/traffic/geo`); hosts are attributed by `RequestHost`
  or by router name via Traefik Manager's `/api/traefik/routers` +
  `/api/routes`. Traefik Manager's own "access log not found" error and
  unmatched routers are surfaced in a status strip on both pages. Both pages
  gained Grafana-style range/auto-refresh controls (URL-backed, no page
  reloads), and `/traffic` gained hand-drawn SVG charts (requests by status
  class, avg/max latency) and a clickable per-service table. See
  `docs/traffic_metrics.md`.
- ~~**Wake for backup.**~~ **Done.** A backup wakes what it needs before
  anything else: policy-level `requires` (e.g. the TrueNAS VM hosting the PBS
  datastore's NFS), per-service `backup_requires`, and — with
  `wake_before_backup` (default on) — the services being backed up. Nodes are held
  awake until the backup ends; a failed wake fails the run before the gate or
  dump. Scheduled policies wake their shared prerequisites once per run.
  Previewed by `GET /api/backup/plan` / `orchestrator-cli backup plan` and a
  *Backup plan* panel on the service page. See [backup_wake.md](backup_wake.md).
- **Follow-up: power nodes back off after a backup they were woken for
  (opt-in).** Today the power manager's idle shutdown handles it once the holds are
  released; an explicit, opt-in "power off what this backup woke" (never anything
  that was already on, never while another hold exists) would save the idle wait.
- **Follow-up: wake-on-hit from the access log.** The wake-on-hit stand-in
  still rides on the Prometheus `TrafficCollector`, so it only works with
  `TRAEFIK_API_URL` set. The access-log collector sees the same "new traffic
  on a proxy host" signal and could drive it too.

## Done: guest gate (DOWN)

**Done.** Every scan of a guest-backed service first checks that its node is
online and its guest is running; if not, the verdict is the new `DOWN` state
and no probe runs (see docs/architecture.md, "Health model"). The backup gate
always requires it (docs/backup_wake.md). Dashboard, service page, `/traffic`
and `orchestrator-cli scan` all show DOWN distinctly. Open follow-ups:

- **Fall back to the power manager** when Proxmox cannot be read: today the
  gate steps aside ("guest state unknown"); `NodeState.power_state` = off could
  still prove DOWN for a node whose Proxmox API is the one that is off.
- **Wake-on-hit from DOWN** already fires through the Prometheus
  `TrafficCollector`; the access-log follow-up above should keep that.

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
the time. **Proxy hosts, then backup policies, got dedicated add/edit pages
+ list filtering** (see the "Small features" entries above) — the service
page (`service_detail.html`) still edits a service inline on its own page
(not a bolted-on-the-bottom list+form split like the other two, so it isn't
the same kind of candidate) and remains open for a similar look if it grows
unwieldy. **The "sub-tabs" half of that deeper redesign is also done**, as a
left sidebar with grouped headings rather than literal tabs (see the "Left
sidebar nav" entry above) — grouping scales better than a flat row of tabs
as more top-level pages (the three infra pages, also added in that entry)
get added. See "The big question: frontend direction" below for why this
stays server-rendered Jinja2 rather than a SPA rewrite.

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

- [x] Proxy host route options: Host header passthrough (fixes redirects to the raw LAN IP), https upstream scheme, wildcard / multi-name certificate domains, skip upstream TLS verify, companion HTTP -> HTTPS redirect router. See docs/proxy_hosts.md.
- [x] Proxy hosts: enable / disable without deleting (Traefik Manager's route toggle, redirect companion included). See docs/proxy_hosts.md.
- [x] Bulk actions: selection checkboxes + action bar on services, nodes, proxy hosts and guests; `POST /api/bulk/*`; `orchestrator-cli bulk`. See docs/bulk_actions.md.
- [ ] Proxy hosts: middlewares picker (auth, headers, rate limit) from Traefik's list.
- [ ] Traefik redirects and middlewares management tabs (list/create/edit Traefik Manager's redirects and middlewares from the orchestrator).
- [ ] Proxy hosts: show/edit a route's raw YAML from the orchestrator (TM `/api/routes/{id}/raw`) for options this form doesn't cover.

