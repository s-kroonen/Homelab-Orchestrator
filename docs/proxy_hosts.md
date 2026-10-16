# Proxy hosts

A **proxy host** is one public route — `hostname → upstream`, served by a
named router (`traefik`, `pangolin`, `npm`, `other`) — that fronts a service
in the registry. The table is `proxy_host`; in `services.yaml` the records sit
under each service's `proxy_hosts:` list.

## What they are today: live-managed when the backend supports it

A `ProxyHost` whose `router_provider` is `traefik` and has an `upstream` is
**live-managed**: creating, editing or deleting it here creates, edits or
deletes the real route on Traefik Manager, not just a local record you
separately reconcile. This replaced the original "pure reference record,
reconciled by a one-way Resync button" model — see
["Live-managed proxy hosts"](#live-managed-proxy-hosts) below for the full
write-through/rollback/backfill behaviour. A `traefik` host with no
`upstream` yet, and every `pangolin`/`npm`/`other` host (no adapter exists
for those backends yet), stay plain reference records: maintained by hand,
nothing pushed to any gateway, same as every `ProxyHost` used to be.
`last_seen_at` is still only ever set by Resync, never by a live-managed
write.

Separately, linking a `ProxyHost` to a `Service` also keeps a small,
surgical maintenance-page fallback middleware in sync on the gateway —
["Maintenance-page fallback"](#maintenance-page-fallback-never-overwrite-an-existing-traefik-config)
further down. This is independent of live-managed route CRUD above (it
predates it) and runs for ANY linked `ProxyHost` regardless of
`router_provider`, fail-open by design (see that section) — unlike route
CRUD, whose failures ARE visible (see below).

That traffic poll can also, per-hostname, trigger a wake for a down
service — this predates the maintenance-page fallback middleware below and
is a stand-in for the SAME outcome via a different mechanism (a poll loop
noticing traffic, rather than Traefik's own `errors` middleware routing the
failing request straight to the maintenance page). The two are independent
and both still work; a service can have either, both, or neither enabled.
See
[traffic_metrics.md#wake-on-hit-a-stand-in-for-a-real-traefik-side-trigger](traffic_metrics.md#wake-on-hit-a-stand-in-for-a-real-traefik-side-trigger).

They exist so both the operator and the orchestrator have one place to look
up "which hostname reaches which service, through what":

- the operator, when debugging a route or planning a cutover
  ([gateway_traefik_migration.md](gateway_traefik_migration.md));
- the orchestrator, e.g. `service list` and `probe add`'s HTTP suggestions
  read `router_provider` and the hostname from here.

## Where to edit them

| Where                           | Writes to          | Persisted how                         |
|---------------------------------|--------------------|---------------------------------------|
| `orchestrator-cli proxy add/list/remove` | `services.yaml` directly | Reconciled into the DB on next boot / Reset |
| Dashboard → **Proxy hosts** (`/proxy-hosts`) — list page | reads the live DB | **Save to YAML** writes it out; **Reset to saved** discards it; **Resync from Traefik** is the bulk-discovery/drift-catch-up path (see below) |
| Dashboard → **Add a proxy host** (`/proxy-hosts/new`) / **Edit** (`/proxy-hosts/{id}/edit`) — dedicated pages | live DB, and (for a `traefik` host with an upstream) the real route | same underlying API as below; these are the normal way to create/edit one now |
| Dashboard → a service's own page (`/services/{slug}`), **Proxy hosts** section | read-only list + a link to `/proxy-hosts/new?service_id=<id>` | same create/edit flow as above, pre-filled for this service |
| `GET/POST/PUT/DELETE /api/proxy-hosts[/{id}]` | live DB, and (for a `traefik` host with an upstream) the real route | the API every page above calls — see ["Live-managed proxy hosts"](#live-managed-proxy-hosts) |
| `orchestrator-cli proxy resync-maintenance` | nothing in the DB; re-syncs the gateway's maintenance middleware for every already-linked host | DB-only operation, no YAML involved — see ["Backfilling existing proxy hosts"](#backfilling-existing-proxy-hosts) below |

**Dedicated add/edit pages, not an inline form.** Creating or editing a
`ProxyHost` used to be a form bolted onto the bottom of `/proxy-hosts` (and
a second, near-duplicate copy of it inline on a service's own page) — "the
same edit on the same page everywhere" was explicitly the wrong pattern,
and this was the first page fixed — backup policies got the same treatment
next (`/backup-policies` + `/backup-policies/new` + `/backup-policies/{name}
/edit`, see `docs/roadmap.md`). `/proxy-hosts` is now list-only (hostnames, upstream,
provider, live status, a hostname-substring filter box, an **Add** button
and a per-row **Edit** link); `/proxy-hosts/new` and
`/proxy-hosts/{id}/edit` are real, dedicated pages, not fragments, that
submit to the same `POST`/`PUT /api/proxy-hosts[/{id}]` as before. The
service page's own inline add-form is gone too — replaced with a link to
`/proxy-hosts/new?service_id=<id>` (pre-filling the service picker) for
consistency with the new pattern, since a second near-duplicate copy of
the same form was exactly the problem being fixed; its own proxy-hosts
list (and Delete) stay, now with an **Edit** link alongside **Delete**.

`proxy_host` is YAML-owned ([architecture.md](architecture.md)), so web edits
behave like any other dashboard registry edit: the "unsaved changes" badge
appears until you Save or Reset. Every web mutation is audited
(`proxy_host.created` / `updated` / `deleted`, target `proxy_host:<hostname>`).

**Every proxy host must link to a service.** The YAML nests proxy hosts under
a service, so an unlinked host would have nowhere to be written — Save to
YAML would silently skip it. The create/update API and the dashboard form
both require `service_id`, matching `orchestrator-cli proxy add <slug>`,
which always takes one too.

One residual case this doesn't close: deleting the linked *service* sets
`service_id` to `NULL` at the DB level (`ON DELETE SET NULL`, the same FK
policy `docs/architecture.md`'s cascade table documents for this column) —
there was never a safe alternative, since the service genuinely no longer
exists. Such an orphaned row goes back to being DB-only (Save skips it)
until you either delete it or edit it to link a different service. The
dashboard page doesn't currently call this case out specially; if you hit
it, the row will look like any other and simply vanish from the next YAML
save unless re-linked.

Validation: hostname is required and must be a bare DNS name (optionally
`*.`-wildcarded; no scheme, port or path), lower-cased on save and unique
across all proxy hosts; `service_id` is required and must reference a real
service; `router_provider` must be one of the enum values above.

## Live-managed proxy hosts

`web/routers/proxy_hosts.py`'s create/update/delete endpoints write through
to `adapters.proxy` (`ProxyAdapter.create_route`/`update_route`/
`delete_route` — `orchestrator.adapters.proxy.base`) whenever **both**
hold:

* `router_provider` is `traefik` — the only backend with a real adapter
  today (`TraefikManagerProxyAdapter`); `pangolin`/`npm`/`other` have none,
  so those rows stay plain reference records, same as every `ProxyHost`
  used to be — not blocked or errored, just skipped;
* `upstream` is non-empty — a route cannot be created on Traefik Manager
  with no backend target. A `traefik` host created/edited with a blank
  `upstream` (a bare hostname reference filled in later, or imported via
  Resync) stays local-only until an `upstream` is set.

Unlike the maintenance-fallback middleware sync further down (a quiet side
effect of an unrelated edit, fail-open by design), route CRUD **is** the
explicit action the admin just took, so a failure is never swallowed:

* **Create**: the `ProxyHost` row is written AND `create_route` is called.
  The returned opaque route reference is stored in
  `ProxyHost.extra["traefik_manager_route_id"]` — the key this app's own
  code looks for; the value itself is `TraefikManagerProxyAdapter`-specific
  (today, a deterministic name derived from the hostname, e.g.
  `orchestrator-app-example-com` — see
  `orchestrator.adapters.proxy.traefik_manager.route_name_for_hostname`).
  **If `create_route` fails, the whole request fails** (`502`) and the
  `ProxyHost` row is never committed — closing this session's DB
  transaction without a commit rolls back the row the same way any other
  failed request in this app already does, so there is no orphaned
  local-only record pointing at nothing real.
* **Update**: the real route is updated FIRST, using the stored
  `extra["traefik_manager_route_id"]` as both the route's name and
  `originalId` (a hostname rename does **not** rename the underlying
  route — the reference is opaque and reused, never re-derived from the
  new hostname). Only once that succeeds are the `ProxyHost` row's fields
  changed; a failure (`502`) leaves both sides exactly as they were. If
  `extra` has no stored reference yet — a host that predates this
  feature, or one whose `router_provider` is only now switching onto
  `traefik` — a route is **created** instead of erroring (same backfill
  behaviour if the stored reference comes back `ProxyRouteNotFound`, e.g.
  the route was deleted by hand on Traefik Manager since).
* **Delete**: the real route is deleted first, via the stored reference,
  then the `ProxyHost` row. A `ProxyRouteNotFound` (already gone on the
  Traefik Manager side somehow) is logged and does **not** block deleting
  the local record — the operator's intent ("this host should no longer
  exist") is still satisfiable. Every other failure (`502`) blocks the
  local delete.

**Known gap**: switching `router_provider` away from `traefik` on an
existing live-managed host does not delete the real route it created —
the stale route is simply no longer touched or tracked (its reference in
`extra` is dropped). There was no safe automatic choice here (the
operator may want to keep the route and manage it by hand going forward),
so this is left as a manual cleanup on Traefik Manager if the route truly
should go away, the same kind of residual gap the "deleting the linked
service" case above already documents.

The maintenance-fallback middleware sync (further down) is **not**
gated by any of this — it runs for any linked `ProxyHost` regardless of
`router_provider`, same as before this feature, since it's a separate,
independent, fail-open integration.

## Drift detection: Traefik Manager preferred, direct Traefik API as fallback

**Traefik Manager** (https://github.com/chr0nzz/traefik-manager) now runs
on the gateway alongside Traefik and is the stated source of truth for
proxy routes — it manages the same one-file-per-service
`/etc/traefik/dynamic/` layout [gateway_traefik_migration.md](gateway_traefik_migration.md)
describes, confirmed compatible with no migration needed. The orchestrator
talks to it through `orchestrator.adapters.proxy` — a `ProxyAdapter` ABC
(`orchestrator.adapters.proxy.base`) with a `DryRunProxyAdapter`
(`dry_run.py`) and a real `TraefikManagerProxyAdapter`
(`traefik_manager.py`), the same `dry_run`/live-implementation shape every
other external system in this app already follows
(`orchestrator.adapters.{power,proxmox,pbs}` — see `docs/architecture.md`).

This used to be two standalone modules kept deliberately outside
`orchestrator.adapters` — a thin REST client and the maintenance-middleware
logic built on top of it — reasoned at the time as "nothing to
dry-run/start/stop here, just short-lived HTTP calls" (there being only one
real proxy backend to support). That reasoning no longer holds now that the
actual requirement is multiple interchangeable proxy backends (today:
Traefik Manager; someday: bare Traefik, NPM, Pangolin) — formalizing the
seam as a real adapter means a second backend is additive later, not a
rewrite of every caller. See `docs/roadmap.md`'s "generalize the adapter
pattern" entry. `TraefikManagerProxyAdapter.start`/`stop` are still no-ops —
every call still builds its own short-lived `httpx.AsyncClient`, same
reasoning as before, it just no longer implies "don't make this an
adapter."

The read path (drift detection below, and Resync further down) now prefers
Traefik Manager's `GET /api/routes` — one call, returning routers,
services *and* middlewares together — over the direct-Traefik-API path
this section used to describe exclusively:

| Condition | Read path used |
|---|---|
| `TRAEFIK_MANAGER_URL` set | Traefik Manager's `GET /api/routes` |
| `TRAEFIK_MANAGER_URL` empty, `TRAEFIK_API_URL` set | The direct Traefik API (`/api/http/routers` + `/api/http/services`), as before |
| Neither set | Disabled — `live_status` is always `unknown`; Resync answers `503` |

**Why the direct-API path was kept as a fallback instead of being retired
outright**, now that Traefik Manager is the stated source of truth: it
costs nothing to keep working, an operator could still run this
orchestrator against a gateway that only exposes Traefik's bare API
without Traefik Manager in front of it (e.g. mid-migration, or a simpler
deployment that never adopts Traefik Manager), and retiring it would mean
deleting working, tested code for no operational benefit. Both paths
degrade identically — see below.

This is strictly **read-only drift detection** — this section never writes
to Traefik or Traefik Manager, and this is not config push/sync. It exists
because, without it, the dashboard can say "this host is configured" while
nothing live actually routes it (typo, config drift, the host never
actually added, and so on).

### Enabling it (Traefik Manager — preferred)

Generate an API key in Traefik Manager's own UI (Settings -> Authentication
-> API Keys — per-device keys are supported) and point the orchestrator at
it:

| Env var | Meaning | Default |
|---|---|---|
| `TRAEFIK_MANAGER_URL` | Base URL of Traefik Manager, e.g. `http://traefik-manager.example.lan:8080`. **Empty disables this path** (falls back to the direct Traefik API below, or to `unknown` if that's unset too). | `""` |
| `TRAEFIK_MANAGER_API_KEY` | The API key from Traefik Manager's UI. Sent as `X-Api-Key`; this also bypasses Traefik Manager's CSRF checks. | `""` |
| `TRAEFIK_MANAGER_VERIFY_TLS` | Verify TLS. Set `false` for a self-signed cert. | `true` |
| `TRAEFIK_MANAGER_TIMEOUT_S` | Request timeout in seconds — short on purpose, same reasoning as `TRAEFIK_API_TIMEOUT_S` below. | `5.0` |

### Enabling it (direct Traefik API — fallback)

1. Start Traefik with its API enabled (static config):

   ```yaml
   api:
     insecure: false  # see the warning below
   ```

   or the CLI equivalent `--api=true`. **Do not set `--api.insecure=true` on
   a public-facing entrypoint.** Traefik's API/dashboard has no authentication
   of its own — anyone who can reach it can read your full router/service
   topology. Bind it to an internal-only entrypoint, a loopback/LAN-only
   listener, or front it with a Traefik `BasicAuth` middleware. If you do add
   a `BasicAuth` middleware in front of the API, pair it with
   `TRAEFIK_API_USERNAME` / `TRAEFIK_API_PASSWORD` below.

2. Point the orchestrator at it:

   | Env var | Meaning | Default |
   |---|---|---|
   | `TRAEFIK_API_URL` | Base URL of the Traefik instance, e.g. `http://traefik.example.lan:8080`. **Empty disables drift checking entirely** — every proxy host reports `live_status: "unknown"`, same as if the poll had failed. | `""` |
   | `TRAEFIK_API_VERIFY_TLS` | Verify the API's TLS certificate. Set `false` for a self-signed cert. | `true` |
   | `TRAEFIK_API_USERNAME` / `TRAEFIK_API_PASSWORD` | Optional HTTP basic auth in front of the API. **Both must be set** to take effect; either alone is ignored. There is no bearer-token option — Traefik's API has no built-in way to check one. | `""` |
   | `TRAEFIK_API_TIMEOUT_S` | Request timeout in seconds. Kept short on purpose: this call runs synchronously on a dashboard page load. | `5.0` |

No orchestrator restart beyond picking up the new `.env` is needed — the poll
happens fresh on each `GET /api/proxy-hosts[/{id}]` call (and on create/update),
not on a background schedule, and nothing is cached across requests.

### How it works

`GET /api/http/routers` on Traefik's API returns every HTTP router, each with
a `rule` string such as:

```
Host(`app.example.lan`) || Host(`app-alt.example.lan`)
```

The orchestrator parses every `Host(...)`/`HostSNI(...)` clause out of every
router's rule (handling multiple hosts OR'd together) into one set of
currently-routed hostnames, then compares each `ProxyHost.hostname` against
it. The result is exposed per host as `live_status` in the API response and
on the dashboard:

| `live_status` | Meaning |
|---|---|
| `matched` | A live Traefik router's rule covers this hostname right now. |
| `missing` | Traefik was reachable, but no router covers this hostname — check for a typo, config drift, or a host that was never actually added to Traefik. |
| `unknown` | Neither `TRAEFIK_MANAGER_URL` nor `TRAEFIK_API_URL` is set, or the configured one's poll failed (network error, timeout, bad response). Deliberately **not** the same as `missing` — this means "we don't know", not "it's not there". |

A poll failure (Traefik down, unreachable, wrong credentials, non-JSON
response, etc.) is logged and degrades to `unknown` for every host — it never
raises into the request handler, so a Traefik outage cannot break the
`/proxy-hosts` dashboard page or the CRUD API.

### What this is not

This drift check itself remains read-only — no SSH, no shared filesystem,
nothing pushed — whichever of the two sources answers it. It does **not**:

- write, create, or modify any route, service, or middleware (that's what
  the separate ["Maintenance-page fallback"](#maintenance-page-fallback-never-overwrite-an-existing-traefik-config)
  feature further down does, deliberately scoped to exactly one appended
  middleware name per route);
- reconcile automatically — an operator still edits `ProxyHost` records by
  hand and decides what to do about a `missing` one.

Traffic, request counts and latency now ARE tracked, via Traefik's
Prometheus metrics endpoint — see [traffic_metrics.md](traffic_metrics.md).
That is a separate, independent read path (`/metrics`, not `/api/http/*`)
with its own poller; it does not change anything described above.

That "real config push" phase has now arrived, scoped narrowly — see
["Maintenance-page fallback"](#maintenance-page-fallback-never-overwrite-an-existing-traefik-config)
below. It is not a general Traefik config editor: it manages exactly one
middleware name on exactly the routes a linked `ProxyHost` identifies, via
Traefik Manager's API rather than SSH or shared storage.

Other options considered and not built here because Traefik's own API is
quicker to turn on and sufficient for this (Prometheus metrics, the other
option originally listed here, now IS built — see
[traffic_metrics.md](traffic_metrics.md)):

- **Access logs** (`accessLog`, JSON format): per-request host, router,
  backend and status; readable by the host runner or a log shipper when
  metrics are too coarse.
- **The file-provider directory itself**: the migration's one-YAML-file-per-
  service convention means the route definitions are already on disk on the
  gateway; parsing them gives configured (not observed) routes without
  touching Traefik at all, but needs filesystem or SSH access this phase
  deliberately avoids.

Pangolin and NPM would need their own equivalents; nothing above applies to
them.

## Resync (manual, admin-triggered write to the DATABASE)

Drift detection above is read-only and automatic (it runs on every page
load). **Resync** is the opposite in both respects: it is the one thing in
this section that writes data into the database (sourced from Traefik
Manager or, as a fallback, Traefik's own API — see "Drift detection"
above), and it only ever runs when an admin clicks the **Resync from
Traefik** button on `/proxy-hosts` (`POST /api/proxy-hosts/resync`).
Nothing changes on a schedule, on page load, or as a side effect of any
other request. This is unrelated to, and does not touch, the
maintenance-page fallback middleware further down, or live-managed route
CRUD above — that's a write to Traefik's own config/routes, not to this
database.

**Resync's role, now that live-managed route CRUD exists above:** it used
to be the *only* way to reach parity between the DB and Traefik — adding
a `ProxyHost` never pushed anything to the gateway, so Resync's "pull
Traefik's current state in" was the one bridge. That is no longer true:
**Add a proxy host**/**Edit** now create/edit the real route directly.
Resync is now the **bulk/catch-up path**: importing routes that exist on
Traefik Manager but have no local `ProxyHost` yet (the "Discovered"
table), and catching real drift — a local record whose `upstream` was
changed by someone editing Traefik Manager directly, bypassing this app
entirely. Nothing about what Resync itself does (below) changed.

### What it does

1. Uses the SAME TM-preferred/direct-API-fallback priority as drift
   detection above to fetch every route's hostname(s), resolved backend
   URL, and router name. Via Traefik Manager this is one `GET /api/routes`
   call; via the direct API it is `/api/http/routers` **and**
   `/api/http/services` (the second to resolve each router's Traefik
   *service* reference — not our own `Service` model, an unfortunate name
   collision — to the backend URL(s) it is currently load-balancing to).
2. For every existing `ProxyHost` whose `hostname` matches a live router:
   - `upstream` is **overwritten** with the backend URL Traefik reports.
     **This intentionally replaces a manually-set `upstream` value** — the
     administrator wants Resync to reflect Traefik's current truth for any
     host it already knows about, trading away manual overrides on matched
     hosts for that guarantee.
   - `last_seen_at` is set to the resync time, whether or not `upstream`
     actually changed.
   - `router_provider` and `extra` are **never** touched by Resync.
3. For every live router hostname with **no** matching `ProxyHost.hostname`,
   nothing is written to the database. `ProxyHost.service_id` is required
   (see above) and Traefik has no concept of our `Service` entity, so
   Resync can never safely invent one. Instead the hostname, its resolved
   upstream and its Traefik router name are returned in the response's
   `discovered` list — a staging area that exists only for the duration of
   that one response, never persisted.
4. The dashboard renders `discovered` as its own table, each row with a
   service picker. Clicking **Add** on a row calls the *existing*
   `POST /api/proxy-hosts` directly — the same endpoint the dedicated
   `/proxy-hosts/new` page uses — pre-filled with that row's
   hostname/upstream plus whichever service was picked (this one row-level
   action stays a direct API call rather than a navigation to that page,
   since it's part of Resync's own bulk flow). There is no second creation
   endpoint, and since `upstream` is already known here, this create IS
   write-through for a `traefik`-provider discovered host (see
   ["Live-managed proxy hosts"](#live-managed-proxy-hosts)).
5. Every resync run is audit-logged (`proxy_host.resynced`) with
   `updated_count`, `discovered_count`, and the matching hostname lists —
   same `orchestrator.audit.log.record` mechanism as every other mutation
   in this file.

If neither `TRAEFIK_MANAGER_URL` nor `TRAEFIK_API_URL` is set, or the
configured one could not be reached, Resync returns `503 Service
Unavailable` with a clear error message naming which source was tried —
unlike the read-only drift check, which silently degrades to `unknown`,
this is a manual action the clicking admin is watching, so a real error is
more useful than a fake success.

### Why `upstream` only, and why it can be overwritten

`router_provider` isn't touched because Traefik's API has no equivalent
concept — it would be invented, not read. `extra` isn't touched because it
holds operator-authored metadata (e.g. middleware lists) that Traefik's API
doesn't expose at all. `upstream`, by contrast, is exactly the piece Traefik
*can* tell us authoritatively — so Resync treats it as the field Traefik
owns once a host is matched, and a manually-typed `upstream` on a matched
host should be expected to be replaced the next time someone clicks Resync.

## Feeding Traefik's view into the service's own HEALTH verdict

Everything above is visible on `/proxy-hosts`, but until now it was
invisible to a service's own HEALTH page: a route that Traefik had quietly
dropped never showed up as a probe result on the service it fronts.

Linking a `ProxyHost` to a `Service` now automatically gives that service a
diagnostic `http` probe that checks the public route *through the gateway*,
named `traefik:<hostname>` — e.g. a `ProxyHost` for `media.example.com`
linked to the `example-media` service gets a probe named
`traefik:media.example.com` with:

```yaml
config:
  url: "<PROBE_PROXY_BASE_URL>/"    # the gateway's LOCAL address
  host_header: media.example.com    # what the proxy routes on
  expect_status: [200, 201, ..., 499]  # any 2xx/3xx/4xx — see below
  verify_tls: false
```

This is exactly the `url` + `host_header` shape every hand-written proxied
HTTP probe in `services.yaml` already uses (see
[probing_through_a_gateway.md](probing_through_a_gateway.md)) — no new probe
kind, just one created for you from data the orchestrator already has.

`expect_status` accepts every 2xx/3xx/4xx (200-499), not just `200`. This
probe answers "is Traefik routing this host to a live backend at all", not
"does the app's homepage render" — it always hits the bare root path `/`,
which for a real app very often redirects, sits behind a login wall, or
404s while the real content lives elsewhere. All of that still proves the
route works. Only a genuine 5xx (Traefik's own "no backend"/"bad gateway"
responses, or the app itself erroring) counts as the route being broken.
If you want this probe to validate something more specific, edit it by hand
same as any other probe — but note that a manual edit is **not** preserved:
the next Resync (or any create/update touching that `ProxyHost`) recomputes
this probe's config from scratch and overwrites it. If you need a
permanently customized check for a proxied route, give it a different name
than `traefik:<hostname>` so this sync never touches it, and leave the
auto-managed one as the route-reachability signal it's meant to be.

`verify_tls` defaults to `false` because this probe hits the gateway's bare
local IP, which commonly carries a self-signed certificate or one that only
validates for the public hostname; a diagnostic probe should not FAIL on a
cert mismatch unrelated to whether the route actually works.

**It never gates anything by default.** The probe is always created with
`required=false` — a service can have several `ProxyHost` records (two
hostnames fronting the same app, say) and none of them individually has to
be reachable for the service to be judged HEALTHY. If you want one specific
route's reachability to actually block backups / mark the service FAILED,
flip `required` to `true` on that one probe from the probe-edit UI on the
service's detail page, the same way you would for any other probe. There is
no separate "severity" concept — `required` is the only knob, reused.

A dead gateway still reads as UNKNOWN downstream, not FAILED, the same way
it already does for every other probe that routes through it — see
[probing_through_a_gateway.md](probing_through_a_gateway.md)'s `depends_on`
section. This probe gets that behavior for free as long as the service also
declares `depends_on: [<gateway service>]`; nothing here is gateway-aware on
its own.

### Lifecycle

The probe tracks the `ProxyHost` it was created from:

- **Create** a linked `ProxyHost` → the probe is created.
- **Rename** the hostname → the probe is renamed/updated in place
  (`host_header` changes); it is never duplicated.
- **Re-link** to a different service → the probe moves: removed from the old
  service, (re)created on the new one.
- **Unlink** (`service_id` cleared) → the probe is removed; there is no
  service left to attach it to.
- **Delete** the `ProxyHost` → its probe is removed.
- **Resync from Traefik** also backfills: every currently-linked
  `ProxyHost` — including ones linked before this feature existed — gets its
  probe ensured as part of the existing per-host loop, whether or not
  Traefik currently has a matching router for it.

Every create/update/removal is audited with the same `probe.created` /
`probe.updated` / `probe.deleted` actions the manual probe-CRUD endpoints
already use, targeting `service:<slug>`.

The whole feature is opt-in: it does nothing (no probe is ever created,
updated or removed) when `PROBE_PROXY_BASE_URL` is unset, same as every
other optional integration in this app.

## Maintenance-page fallback (never overwrite an existing Traefik config)

The orchestrator already renders a self-refreshing "starting up" page and
fires the wake pipeline at `GET /maintenance/{service_slug}`
(`orchestrator.web.routers.maintenance`) — but until now, Traefik had to be
hand-configured, per service, to actually send a visitor there when the
real backend is down. This feature closes that gap automatically for every
`ProxyHost` linked to a `Service`, using Traefik's own `errors` middleware
and Traefik Manager's write API — see
`TraefikManagerProxyAdapter.ensure_maintenance_fallback`/
`remove_maintenance_fallback` (`orchestrator.adapters.proxy.
traefik_manager`) for the implementation this section describes. The
create/rename/re-link/unlink lifecycle orchestration around those two
calls — which hostname/slug is old vs. new — lives in
`web/routers/proxy_hosts.py`'s `_sync_maintenance_fallback`, since the
adapter's two methods are deliberately simple, single-purpose verbs that
don't know about "old" values themselves (see the lifecycle section
below).

### Enabling it

Two settings (see `docs/architecture.md` for the full table):

* `TRAEFIK_MANAGER_URL` + `TRAEFIK_MANAGER_API_KEY` — also used for the
  read-path swap above, and the ONE hard requirement for `PROXY_ADAPTER` to
  resolve to `TraefikManagerProxyAdapter` at all (`PROXY_ADAPTER=traefik_manager`
  with this left blank falls back to `DryRunProxyAdapter`, loudly — see
  `orchestrator.adapters.factory.build_adapters`).
* `ORCHESTRATOR_PUBLIC_URL` — this orchestrator's own address **as
  reachable from the gateway VM**, e.g. `http://10.0.0.5:8000`. This is
  what Traefik's `errors` middleware sends the failed request to. Unlike
  `TRAEFIK_MANAGER_URL`, leaving this blank does NOT change which adapter
  is selected — it only makes this one feature's `ensure_maintenance_fallback`/
  `remove_maintenance_fallback` calls a no-op; drift detection and Resync
  still work.

Either unset and this entire feature is a no-op — no shared backend
service, no middleware, no injection, same "empty disables it" convention
as every other optional integration in this app.

### What it builds, and why

1. **One shared backend service**
   (`orchestrator-maintenance-backend`), pointing at
   `ORCHESTRATOR_PUBLIC_URL`, created once and reused by every service's
   middleware. Traefik's `errors` middleware invokes a Traefik *service*
   directly — it does not need a router to reach it — but Traefik
   Manager's `POST /save` endpoint is modeled around creating a
   router+service PAIR, with no supported way to write a bare service with
   no router.

   **The "service with no router" resolution**: create a minimal,
   deliberately-never-matched router alongside the service, whose
   `httpRule` targets a hostname under the reserved `.invalid` TLD
   (RFC 2606: "and are guaranteed not to be installed" as a real DNS name,
   ever) rather than relying on an empty/absent `entryPoints` value, whose
   exact behaviour (no binding at all, vs. falling back to Traefik
   Manager's documented HTTP default of `https`) isn't nailed down by its
   docs. Even in the worst case — the router ends up bound to a real
   entrypoint — no real request will ever carry
   `Host: orchestrator-maintenance-backend.invalid`, so it can never
   actually serve live traffic. This is belt-and-suspenders against that
   one documentation gap, and is called out explicitly here because it's
   the one judgment call in this feature that couldn't be fully verified
   against a live Traefik Manager instance; the exact request shape sent
   is pinned down by a test against a mocked API
   (`tests/test_proxy_adapter_traefik_manager.py`).

   Idempotent: checked via `GET /api/routes`'s `services.http` list before
   creating — a second sync (another `ProxyHost` create, a resync, a
   restart) makes no write at all once it exists.

2. **One middleware per service** (`maintenance-<slug>`), of Traefik's
   `errors` type:

   ```yaml
   errors:
     status: ["500-599"]
     service: orchestrator-maintenance-backend
     query: /maintenance/<slug>
   ```

   `status` covers the full 5xx range — Traefik's own convention for "the
   backend is unreachable at all", since a fully down backend surfaces to
   this middleware as a synthetic 5xx (Traefik's own "no server
   available"/"bad gateway" response). This alone is sufficient to catch a
   sleeping/off backend; a `forwardAuth`-based health check was considered
   and explicitly rejected as unnecessary complexity for the same outcome.
   `query` is the literal path `/maintenance/<slug>` — exactly the existing
   `GET /maintenance/{service_slug}` route, unmodified; nothing there
   needed to change for this feature. `service` always points at the one
   shared backend from step 1.

   Created/updated via `POST /save-middleware`. Looked up by name in
   `GET /api/routes`'s `middlewares` list to decide create vs. update; the
   content is always rewritten on an update so a renamed service's stale
   `query` (pointing at an old slug) can never linger.

3. **Injection into every route matching the `ProxyHost`'s hostname** — the
   one hard, non-negotiable requirement of this feature: **the middleware
   name is appended to that route's `middlewares:` list via the raw-YAML
   read-modify-write pair**, never `POST /save`:

   ```
   GET /api/routes/{id}/raw  ->  parse YAML
                              ->  append the name to middlewares: if absent
                              ->  POST /api/routes/{id}/raw with the full content
   ```

   Every other field on that route — its rule, entrypoints, TLS options,
   and every OTHER middleware already on it, hand-authored ones
   included — survives byte-for-byte except for that one appended list
   entry. `POST /save`'s field-by-field semantics were deliberately NOT
   used here: a client that doesn't echo back every field it didn't touch
   risks silently dropping ones it doesn't know about (see `/save`'s own
   docs on this exact hazard for its `backendsJson*`/sticky/healthCheck
   fields) — exactly the "must not overwrite an existing Traefik config"
   failure mode the operator was explicit about avoiding. Idempotent: the
   name is never appended twice, so re-running (another `ProxyHost` edit, a
   container restart, a resync) is always safe.

   `tests/test_proxy_adapter_traefik_manager.py` asserts this round-trip directly:
   every other field on a route carrying a hand-authored middleware
   survives an injection untouched, and a second run makes no further
   write at all.

### Lifecycle

Mirrors the existing auto-managed Traefik probe's lifecycle (above) and the
same trigger points in `web/routers/proxy_hosts.py`:

* **Create** a linked `ProxyHost` → ensure the shared backend service +
  this service's middleware exist, inject the middleware name into every
  route matching the hostname.
* **Rename** the hostname (same service) → remove the (unchanged,
  slug-named) middleware name from the OLD hostname's routes, inject it
  into the NEW hostname's routes.
* **Re-link** to a different service → the middleware name is slug-based,
  so a different service means a different middleware: remove the OLD
  slug's middleware name from the hostname's routes, ensure the NEW slug's
  middleware, inject it.
* **Unlink** (`service_id` cleared) or **delete** the `ProxyHost` → remove
  the middleware name from the (old) hostname's routes.

**On unlink/delete, the middleware DEFINITION itself is deliberately left
in place** (orphaned-but-harmless), not deleted. Deleting it would first
need to confirm no OTHER route still references it
(`POST /delete-middleware/{name}` answers `409` otherwise, naming the
routers that do), and the next create/re-link for the same slug would just
recreate it anyway — so this module trades a harmless unreferenced
middleware definition for not having to do that extra round trip. An
operator who wants the gateway's config file fully clean of a removed
service's middleware can delete it by hand, or via Traefik Manager's own
UI, at any time — nothing reads or re-creates a middleware definition
except by its own slug-derived name.

Every create/update/injection/removal is audit-logged
(`backend_service.created`, `middleware.created` / `middleware.updated` /
`middleware.injected` / `middleware.removed`), the same
`orchestrator.audit.log.record` mechanism as every other mutation in this
file, target `traefik_manager:<name>`.

### Fail-open, by design

Unlike Resync (an explicit, admin-watched action that surfaces a real
`503` on failure), this sync is a *side effect* of an ordinary `ProxyHost`
create/update/delete — the same kind of request this app has always
allowed to succeed even when Traefik is completely unconfigured. A Traefik
Manager outage (or misconfiguration) is caught and logged here, never
raised into that request: editing proxy hosts in the database must keep
working even when the gateway integration is unreachable. An operator who
needs to know whether a particular sync actually happened should check the
audit log for the expected `middleware.*` entries, or the activity page.

### Backfilling existing proxy hosts

Because this sync only runs on a `ProxyHost` create/update/delete/re-link,
two situations leave a host without the middleware, with no automatic way
to notice or fix it:

1. **The settings were just turned on.** `TRAEFIK_MANAGER_URL` and
   `ORCHESTRATOR_PUBLIC_URL` were unset when a `ProxyHost` was created, and
   got set later (see docs/roadmap.md's "Quick fix" entry) — every host
   that already existed at that point never had a sync attempt made for
   it, since the opt-in check short-circuited before anything else ran.
2. **The host predates this feature entirely** — created before Traefik
   Manager integration existed in this codebase at all.

Either way, the fix is the same: re-run the sync for every already-linked
host once, with its current `(hostname, service_slug)` as both the old and
new value — the same no-op "re-sync" shape the create/update lifecycle
hooks above already use for a host whose hostname and service haven't
changed. Hand-editing every row once (an edit that doesn't actually change
anything, just to re-trigger the hook) works, but doesn't scale past a
couple of hosts, so there's a dedicated command instead:

```bash
orchestrator-cli proxy resync-maintenance
```

Loops every `ProxyHost` row with a linked `service_id`, calls
`ProxyAdapter.ensure_maintenance_fallback` (via `build_adapters(settings).proxy`)
once per host (same as above — a no-op without `TRAEFIK_MANAGER_URL`/
`ORCHESTRATOR_PUBLIC_URL`, printing a message saying so rather than doing
anything), and prints what happened per host.
Unlike `proxy add/list/remove`, this operates on the **live database**
directly rather than `services.yaml` — there is nothing to reconcile or
save afterwards, since nothing about a `ProxyHost` row itself changes; only
the gateway's middleware config does.

Every create/update/injection/removal this backfill performs is
audit-logged exactly like the lifecycle-triggered sync above (same
`middleware.*`/`backend_service.*` action names, same
`orchestrator.audit.log.record` mechanism, target `traefik_manager:<name>`)
— only the actor differs: `cli`, the same convention `backup`/`wake`
already use for a CLI-triggered action, vs. the dashboard admin's username
for a sync triggered by an edit there.

Safe to run any time, including with nothing to backfill: a host whose
middleware is already correctly injected gets a no-op pass (no write, no
audit entry) — see the "Idempotent" notes on injection/removal above. Also
safe to run repeatedly after a Traefik Manager migration, to make sure
every linked host's middleware exists on the new instance.

### What this is not

* Not a general Traefik/Traefik-Manager config editor — it manages exactly
  one middleware name, on exactly the routes matching one `ProxyHost`'s
  hostname, and one shared backend service. Nothing else on those routes,
  and no OTHER route, is ever touched.
* Not a health check of its own — it relies entirely on Traefik's own
  `errors` middleware semantics (a dead backend already reads as a 5xx to
  it); no separate polling or `forwardAuth` call is made by this feature.
* Not required for the maintenance page itself to work — `GET
  /maintenance/{service_slug}` has existed since an earlier phase and keeps
  working exactly as before however a visitor arrives at it (a hand-wired
  Traefik `errors` middleware, a reverse proxy elsewhere, or a direct hit).
  This feature only automates wiring Traefik up to send visitors there.
