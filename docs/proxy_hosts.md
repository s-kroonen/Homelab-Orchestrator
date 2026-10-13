# Proxy hosts

A **proxy host** is one public route — `hostname → upstream`, served by a
named router (`traefik`, `pangolin`, `npm`, `other`) — that fronts a service
in the registry. The table is `proxy_host`; in `services.yaml` the records sit
under each service's `proxy_hosts:` list.

## What they are today: a manual reference

These records are **maintained by hand**. Nothing in this repo talks to
Traefik (or Pangolin, or NPM): there is no API client, no route discovery, no
metrics or access-log reader. Creating, editing or deleting a proxy host here
changes nothing on the gateway, and `last_seen_at` is never set automatically.

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

## What making them live would take (not designed yet)

Turning these from reference records into observed state needs the gateway to
expose *something* this repo can read. Any one of these would do; none is
enabled on the gateway today, and the integration itself is deliberately
left for a future PR:

- **Traefik API / dashboard** (`api: {}` in static config, ideally on an
  internal-only entrypoint): `/api/http/routers` and `/api/http/services`
  list live routers, rules, backends and their status — enough to populate or
  diff these records and set `last_seen_at`.
- **Prometheus metrics** (`metrics.prometheus` in static config): per-router
  and per-service request counts, status codes and latency — the basis for
  traffic/"last seen" monitoring rather than just route existence.
- **Access logs** (`accessLog`, JSON format): per-request host, router,
  backend and status; readable by the host runner or a log shipper when
  metrics are too coarse.
- **The file-provider directory itself**: the migration's one-YAML-file-per-
  service convention means the route definitions are already on disk on the
  gateway; parsing them gives configured (not observed) routes without
  touching Traefik at all.

Pangolin and NPM would need their own equivalents; nothing above applies to
them.
