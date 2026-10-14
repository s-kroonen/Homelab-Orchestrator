# Proxy hosts

A **proxy host** is one public route — `hostname → upstream`, served by a
named router (`traefik`, `pangolin`, `npm`, `other`) — that fronts a service
in the registry. The table is `proxy_host`; in `services.yaml` the records sit
under each service's `proxy_hosts:` list.

## What they are today: a manual reference

These records are **maintained by hand**. Creating, editing or deleting a
proxy host here changes nothing on the gateway, and `last_seen_at` is never
set automatically. The orchestrator can optionally *read* Traefik's router
list to flag drift (below), and optionally poll Traefik's Prometheus metrics
to show per-service/per-host traffic (see
[traffic_metrics.md](traffic_metrics.md)) — both entirely read-only. Nothing
in this repo ever writes to Traefik, Pangolin or NPM.

That traffic poll can also, per-hostname, trigger a wake for a down
service — a stand-in until write access to Traefik's config exists to wire
up a real Traefik-side trigger. See
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
| Dashboard → **Proxy hosts** (`/proxy-hosts`) | live DB only | **Save to YAML** writes it out; **Reset to saved** discards it |
| `GET/POST/PUT/DELETE /api/proxy-hosts[/{id}]` | live DB only | same as the page — it's what the page calls |

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

## Drift detection against Traefik (read-only)

The orchestrator can optionally poll Traefik's own REST API and flag any
`ProxyHost` whose hostname has no matching **live** router. This is strictly
**read-only drift detection** — the orchestrator never writes to Traefik, and
this is not config push/sync. It exists because, without it, the dashboard
can say "this host is configured" while Traefik silently has no matching
route (typo, Traefik config drift, the host never actually added to Traefik,
and so on).

### Enabling it

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
| `unknown` | `TRAEFIK_API_URL` is not set, or the poll failed (network error, timeout, bad response). Deliberately **not** the same as `missing` — this means "we don't know", not "it's not there". |

A poll failure (Traefik down, unreachable, wrong credentials, non-JSON
response, etc.) is logged and degrades to `unknown` for every host — it never
raises into the request handler, so a Traefik outage cannot break the
`/proxy-hosts` dashboard page or the CRUD API.

### What this is not

This is the smallest first step towards closing the gap between "configured
here" and "actually routing in Traefik" — read-only, no SSH, no shared
filesystem, nothing pushed. It does **not**:

- write, create, or modify anything in Traefik;
- reconcile automatically — an operator still edits `ProxyHost` records by
  hand and decides what to do about a `missing` one.

Traffic, request counts and latency now ARE tracked, via Traefik's
Prometheus metrics endpoint — see [traffic_metrics.md](traffic_metrics.md).
That is a separate, independent read path (`/metrics`, not `/api/http/*`)
with its own poller; it does not change anything described above.

A later phase may add real config push once SSH access or shared storage
exists between the orchestrator and the Traefik host — not designed yet, and
not assumed to be coming.

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

## Resync from Traefik (manual, admin-triggered write)

Drift detection above is read-only and automatic (it runs on every page
load). **Resync** is the opposite in both respects: it is the one thing in
this file that writes data sourced from Traefik into the database, and it
only ever runs when an admin clicks the **Resync from Traefik** button on
`/proxy-hosts` (`POST /api/proxy-hosts/resync`). Nothing changes on a
schedule, on page load, or as a side effect of any other request.

### What it does

1. Calls Traefik's `/api/http/routers` **and** `/api/http/services` APIs
   (the second to resolve each router's Traefik *service* reference — not
   our own `Service` model, an unfortunate name collision — to the backend
   URL(s) it is currently load-balancing to).
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
   `POST /api/proxy-hosts` — the same endpoint the manual "Add a proxy
   host" form uses — pre-filled with that row's hostname/upstream plus
   whichever service was picked. There is no second creation endpoint.
5. Every resync run is audit-logged (`proxy_host.resynced`) with
   `updated_count`, `discovered_count`, and the matching hostname lists —
   same `orchestrator.audit.log.record` mechanism as every other mutation
   in this file.

If `TRAEFIK_API_URL` is unset, or Traefik could not be reached, Resync
returns `503 Service Unavailable` with a clear error message — unlike the
read-only drift check, which silently degrades to `unknown`, this is a
manual action the clicking admin is watching, so a real error is more useful
than a fake success.

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
