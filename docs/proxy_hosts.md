# Proxy hosts

A **proxy host** is one public route — `hostname → upstream`, served by a
named router (`traefik`, `pangolin`, `npm`, `other`) — that fronts a service
in the registry. The table is `proxy_host`; in `services.yaml` the records sit
under each service's `proxy_hosts:` list.

## What they are today: a manual reference

These records are **maintained by hand**. Creating, editing or deleting a
proxy host here changes nothing on the gateway, and `last_seen_at` is never
set automatically. The orchestrator can optionally *read* Traefik's router
list to flag drift (below) — that is the only thing in this repo that talks
to Traefik, Pangolin or NPM, and it is read-only: there is no route
discovery, metrics or access-log reader, and nothing here ever writes to the
gateway.

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
- track traffic, request counts or latency (that's Prometheus metrics,
  still a possible future addition, see below);
- reconcile automatically — an operator still edits `ProxyHost` records by
  hand and decides what to do about a `missing` one.

A later phase may add real config push once SSH access or shared storage
exists between the orchestrator and the Traefik host — not designed yet, and
not assumed to be coming.

Other options considered and not built here because Traefik's own API is
quicker to turn on and sufficient for this:

- **Prometheus metrics** (`metrics.prometheus` in static config): per-router
  and per-service request counts, status codes and latency — the basis for
  traffic/"last seen" monitoring rather than just route existence.
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
