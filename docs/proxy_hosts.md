# Proxy hosts

A **proxy host** is one public route — `hostname → upstream`, served by a
named router (`traefik`, `pangolin`, `npm`, `other`) — that fronts a service
in the registry. The table is `proxy_host`; in `services.yaml` the records sit
under each service's `proxy_hosts:` list.

## What they are today: a working copy that is pushed to Traefik Manager

A `ProxyHost` whose `router_provider` is `traefik` and has an `upstream` is
**live-managed**: the orchestrator owns a route for it on Traefik Manager. But
**the database is the working copy and Traefik Manager is written only when
you push** - on **Save to YAML**, on **Push now** (`POST /api/push`) or with
`orchestrator-cli proxy push`. Adding, editing, enabling/disabling or deleting
a host here (UI, API, bulk actions, CLI bulk) changes the DB and nothing else;
every read-only check (hostname, upstream, entry points, resolver, route
options, conflicts, the redirect middleware) still runs at edit time. See
["Pushing to Traefik Manager"](#pushing-to-traefik-manager) for the sync
states, the push order and what "detached" means. A `traefik` host with no
`upstream` yet, and every `pangolin`/`npm`/`other` host (no adapter exists for
those backends yet), stay plain reference records: nothing is ever pushed for
them. `last_seen_at` is still only ever set by Resync.

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
| `orchestrator-cli proxy add --entry-point websecure --cert-resolver letsencrypt` | `services.yaml`; validated first with the same rules as the API (entry points checked against the live Traefik when readable) | As above. `orchestrator-cli proxy entrypoints` lists what the manager reports (exit 1 + a message when unavailable); non-interactive runs use the default rule when no `--entry-point` is given |
| Dashboard → **Data sources → Traefik Manager** (`/sources#traefik`) | pulls routes into records (import / resync) and rewrites old routes into the standard shape — see [sources.md](sources.md) | the import button and the *Discovered* list moved here from the proxy hosts page |
| Dashboard → **Proxy hosts** (`/proxy-hosts`) — list page | reads the live DB (+ a push-status badge per host) | **Save to YAML** writes it out **and pushes** pending changes to Traefik Manager; **Push now** pushes only; **Reset to saved** discards unsaved edits and pending deletions; **Resync from Traefik** is the bulk-discovery/drift-catch-up path (see below) |
| Dashboard → **Add a proxy host** (`/proxy-hosts/new`) / **Edit** (`/proxy-hosts/{id}/edit`) — dedicated pages | live DB only ("Saved here. Pushed to Traefik Manager on Save to YAML (or Push now).") | same underlying API as below; these are the normal way to create/edit one now |
| Dashboard → a service's own page (`/services/{slug}`), **Proxy hosts** section | read-only list + a link to `/proxy-hosts/new?service_id=<id>` | same create/edit flow as above, pre-filled for this service |
| `GET/POST/PUT/DELETE /api/proxy-hosts[/{id}]` | live DB only; list/get carry `sync_state` + `sync_changes` | the API every page above calls — see ["Live-managed proxy hosts"](#live-managed-proxy-hosts) |
| `GET /api/push/status`, `POST /api/push` | status: nothing. Push: Traefik Manager (and any other registered pusher) | **Push now** / the CLI use these — see ["Pushing to Traefik Manager"](#pushing-to-traefik-manager) |
| `orchestrator-cli proxy status` / `proxy push [--yes]` | status: nothing. Push: Traefik Manager | the same engine in-process; `status` exits 1 when anything is not in sync |
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

A host is live-managed when **both** hold:

* `router_provider` is `traefik` - the only backend with a real adapter today
  (`TraefikManagerProxyAdapter`); `pangolin`/`npm`/`other` rows stay plain
  reference records;
* `upstream` is non-empty - a route cannot be created on Traefik Manager with
  no backend target.

What the endpoints do now (`web/routers/proxy_hosts.py`):

* **Create** validates everything read-only (see below), stores the row and the
  *planned* route reference in `extra["traefik_manager_route_id"]`
  (`ProxyAdapter.route_ref_for`; for Traefik Manager the deterministic name
  `orchestrator-app-example-com`, so the saved YAML already carries it and a
  later push does not make the registry dirty). The host is `pending_create`.
  Re-creating a hostname that was just deleted (its route not yet removed)
  takes that route over instead of conflicting with it.
* **Update** validates, stores, keeps the stored reference (a hostname rename
  does **not** rename the route) and leaves the host `pending_update`. A host
  that stops being live-managed (provider changed, upstream cleared) gets a
  pending deletion for the route this orchestrator created. There is no longer
  an "enable it first" refusal for a disabled host: the push orders the
  operations safely.
* **Delete** removes the row and, for a route this orchestrator created that
  Traefik Manager really has, records a **pending deletion**
  (`{hostname, route_ref, with_redirect}` in the `Setting` row
  `proxy.pending_deletes`). Routes that were adopted or hand-written are never
  deleted. **Reset to saved** clears the pending deletions (the YAML reconcile
  recreates the hosts).
* **Enable / disable** and the bulk actions only flip `ProxyHost.enabled`.

## Pushing to Traefik Manager

`orchestrator.registry.proxy_push` (the `proxy` pusher) brings Traefik Manager
in line with the DB rows. It runs on **Save to YAML** (after the YAML was
written and committed - a push failure never undoes the save and is reported
in the response), on **Push now**, and from the CLI.

### What is remembered

`ProxyHost.pushed_sig` (migration `0015_proxy_host_pushed_sig`, default empty =
unknown) is a fingerprint of everything pushed for the host - hostname,
upstream, entry points, cert resolver, route options, linked service (the
maintenance fallback depends on it) - followed by `:1`/`:0` for enabled. It is
recorded only after every step for that host succeeded, is runtime state (not
in `services.yaml`) and survives Reset/boot reconcile by hostname.

### Sync states (`sync_state` / `sync_changes` on every list/get payload)

| State | Meaning | A push does |
|---|---|---|
| `in_sync` | the route exists; nothing readable differs | nothing |
| `pending_create` | live-managed, Traefik Manager has no route (never pushed, or removed there) | creates it |
| `pending_update` | edited here since the last push (fingerprint differs); for a host never pushed from here: readable fields differ from Traefik Manager | updates it |
| `detached` | **our fingerprint is unchanged but Traefik Manager's live state differs** - someone changed it there | puts our state back |
| `external` | the hostname's route is not ours (adopted by Resync, hand-written, or no route reference) | only its on/off state (and the maintenance fallback); **content is never overwritten or deleted** |
| `local` | not live-managed | nothing (unless a hand-written route serves the hostname and the maintenance fallback is not applied yet) |

**Exactly what "detached" compares** (only what can be read back from
`GET /api/routes`): the enabled/disabled state (the `-redirect` companion
included when `http_redirect` is on; partly-off counts as a difference), the
upstream (normalised `scheme://host:port`, any difference in the server list),
the entry points (as sets, when both sides report them) and - when
`http_redirect` is on - a missing companion router. Cert resolver, route
options and middlewares are not readable, so they are tracked by the
fingerprint only.

`sync_changes` holds human strings such as `upstream differs: Traefik Manager
has http://a, we have http://b`. Pending deletions are listed separately.

### Apply order (per host, idempotent, one host failing never stops the others)

1. **create** the route if missing (the conflict refusal is repeated; if
   Traefik rejects the new router after verification it is deleted again and
   the host reports the error);
2. **enable** it first when we want it on and Traefik Manager has it parked;
3. **update** the content (`update_route`) when pending/detached;
4. ensure the **maintenance-page fallback** for the service (a route rewrite
   drops the middleware);
5. **disable** LAST (route + redirect companion) when we want it off - and only
   if something is still on;
6. **verify** enabled routes with `verify_route` (skipped for disabled ones);
7. record `pushed_sig`.

Then pending deletions are processed (route + redirect deleted, "not found"
tolerated, tombstone cleared) and the maintenance fallback is removed for them.
The result is a per-item list `{target, hostname, action, ok, message}`.

### API and CLI

* `GET /api/push/status` -> `{"targets": [{"name": "proxy", "reachable": bool,
  "in_sync": n, "pending": [{"hostname", "state", "changes"}], "detached":
  [...], "pending_deletes": [...]}]}`.
* `POST /api/push` `{"targets": ["proxy"], "hostnames": [...], "dry_run": false}`
  -> `{"results": [...], "summary": {"attempted", "ok", "failed"}}`.
  `dry_run: true` returns what would be done without writing (the UI confirm).
* `POST /api/push` also takes `"move_config_files": ["host", ...]`: after the
  normal push, those hosts' existing routes are **moved** into the config file
  they resolve to (see [Config files](#config-files-where-a-route-lives)). It is
  the only way a push ever moves a route.
* `POST /api/registry/save` -> the original keys plus `push_results` and
  `push` (`{"attempted","ok","failed"}`).
* `orchestrator-cli proxy status` (exit 1 unless in sync) and
  `orchestrator-cli proxy push [--yes] [--hostname H] [--move-config-file H]`
  (exit 1 if an item failed). Under `DRY_RUN=true` / `PROXY_ADAPTER=dry_run` the push goes through
  the dry-run adapter, which only records what it was given.

### Other outside services

The engine is a tiny protocol (`orchestrator.registry.push`): a `Pusher` has a
`name`, `status(session, adapters) -> PushStatus` and `apply(session, adapters,
only=None, *, dry_run=False, actor=...) -> list[PushResult]`; `register_pusher`
adds it to the registry. Save to YAML, `/api/push` and the status endpoint run
every registered pusher, so a future backup or power adapter that wants the same
"edit locally, push on save" behaviour only implements the protocol.

### Traefik Manager with several config files (`file::name` ids)

With `MULTI_CONFIG` Traefik Manager gives every route in `GET /api/routes`
`id = "<configfile>::<name>"` and `name = "<name>"` (one file: they are equal).
Its toggle only acts on the listed id - a bare name is a **silent no-op that
still answers 200**, and enabling only finds a route whose disabled-routes
record is keyed by that id. The adapter therefore:

* keeps `RouteInfo.route_id` / `config_file` and resolves a stored or bare
  reference against `GET /api/routes` (`id == ref` or `name == ref`; if several
  files hold the same name, the one whose rule matches the host);
* toggles by the real id and **reads the route back**: if `enabled` is not what
  was asked it raises `Traefik Manager accepted the request but the route is
  still enabled/disabled` (also for the redirect companion, undoing the first
  toggle);
* edits (`/save`) with `originalId` = the id and `configFile` = the route's own
  file (otherwise it would write a second copy into the default file) and
  deletes (`/delete/<id>`) with `configFile` set, so a same-named router in
  another file survives.

### Config files: where a route lives

Traefik Manager (TM) writes every route into one of its dynamic-config files.
Until now every route this orchestrator created was posted with no `configFile`,
so it landed in TM's **default** file - the one you see as `_shared`. The
convention here is one file per service (`<service-slug>.yml`) with `_shared.yml`
kept for shared middlewares, so a **new** route is now created in the file named
by:

1. the host's own **Config file** (form field, `route_options.config_file`), else
2. the `TRAEFIK_MANAGER_CONFIG_FILE` template - default **`{service}.yml`**.
   `{service}` is the linked service's slug, `{hostname}` the hostname with dots
   turned into dashes (`media.example.com` -> `media-example-com`). A missing
   `.yml` / `.yaml` extension is added. **Blank** = TM's own default file, as
   before.

The form shows the resolved default as the field's placeholder and the known
files as suggestions (a datalist); `GET /api/proxy/entrypoints` returns them as
`config_files`, `config_files_known`, `config_files_can_create`,
`config_file_template` and - for `?service=<slug>&hostname=<name>` -
`default_config_file`. The files are read from TM's `GET /api/configs`
(`{"files": [{"label", "path"}], "configDirSet": bool}`); on a TM without that
endpoint the `configFile` values of `GET /api/routes` are used instead.

**What TM does with a file it does not know** (read from its `/save` and
`resolve_config_path`):

* a name in its `CONFIG_PATHS` / config directory -> that file;
* a bare name while TM runs with **`CONFIG_DIR`** (`configDirSet: true`) -> TM
  **creates** `<name>.yml` in that directory (and registers it);
* anything else - a fixed `CONFIG_PATHS` list without that file, or a name with a
  slash - -> TM quietly falls back to its **default file** and answers 200. The
  route would land somewhere nobody asked for.

So the orchestrator checks the name itself and never posts one TM would replace:
with a fixed `CONFIG_PATHS` it fails with *"Traefik Manager has no config file
`X` and cannot create it: it can only write to the files in its CONFIG_PATHS
(`a.yml, b.yml`). Add X to CONFIG_PATHS (or switch to CONFIG_DIR) or pick one of
the existing files."* - in the form (`422`, field `config_file`, when you typed it
there) and at push time (the item fails, nothing is written, **never a silent
fall-back to `_shared`**). With `CONFIG_DIR` the file is simply created. A file you
just added to TM is picked up without waiting for the 30 s catalog cache. After a
create into a named file the route is read back; if TM put it elsewhere it is
removed again and the push reports it.

A TM with **one fixed config file** has nothing to choose, so the *template* is not
applied there (the route goes into that file); a name you typed yourself is still
checked.

The HTTP -> HTTPS companion router (`<route>-redirect`) is always created in the
**same file as its main route**.

**Existing routes stay where they are.** Editing or pushing a route that already
lives in some file posts that file back (`originalId` + `configFile` of the route
itself), so an edit never relocates it. Changing a host's wanted file alone is not
a pending change (it is not part of the route's content). The list and the edit
form show the file each route is in (`tm_config_file`, from `/api/routes`) and
**flag** a route in `_shared.yml` - or any file other than the one the host
resolves to - with an informational note, never an error. Adopted (hand-written,
`external`) routes are flagged too but never moved.

**Move to configured file** is the opt-in way to relocate one: the edit form's
button, the list's **Move**, `POST /api/proxy-hosts/{id}/move-config-file`,
`POST /api/push` with `move_config_files`, or `orchestrator-cli proxy push
--move-config-file HOST`. It re-saves the route into the resolved file with TM's own
move behaviour - `/save` with `isEdit=true`, `originalId=<old file>::<name>` and
`configFile=<new file>` (TM then removes the router, and its service unless
another router shares it, from the old file) - the redirect companion **first**
(it references the main route's service, which would otherwise count as shared and
stay behind), then the main route. The result is **read back**: exactly one copy of
each router, in the new file, or the move fails and says where it found them.
Notes: the route is rewritten from our record like any update, so middlewares
added by hand in TM are not carried (the maintenance-page fallback is put back);
a route that is switched off (parked by TM outside any file), a disabled host and a
route that is not ours are refused with a message.

### Entry points and certificate resolver

Every live-managed host carries two stored fields, chosen by the operator and
**always sent explicitly** to Traefik Manager's `/save` — on create *and* on
every edit:

* `entry_points` — the Traefik entry points the router listens on
  (`web`, `websecure`, ...). Sent as the comma-joined `entryPoints` field.
* `cert_resolver` — the certificate resolver that issues the route's TLS
  cert (empty = none). Sent as Traefik Manager's `certResolver` field: the
  resolver's name, or `__none__` (TLS block without a resolver), or
  `__disabled__` (no `tls` block at all — what a route on plain `web` /
  `:80` gets when no resolver is chosen).

**Why this exists.** Traefik Manager's `/save` turns an empty `entryPoints`
into its own default, `https`, and an empty `certResolver` into the first
resolver it knows. This orchestrator used to post both empty, so a route
created on a Traefik with entry points `web` / `websecure` / `traefik` was
accepted and then shown by Traefik Manager as broken —
`entryPoint "https" doesn't exist · no valid entryPoint for this router` —
and every *edit* reset a working route's entry points to `https` again. The
orchestrator now never posts a blank value; an empty selection is refused
before anything is written, in the adapter as well as the API.

**Where the choices come from.** `GET /api/proxy/entrypoints` (admin) returns
what the live proxy reports, cached for ~30 s (the form's
"Refresh from Traefik Manager" button and Resync bust the cache):

```json
{"entrypoints": [{"name": "web", "address": ":80"}, ...],
 "cert_resolvers": ["letsencrypt", "letsencrypt-dns"], "cert_resolvers_known": true,
 "cert_resolver_sources": {"letsencrypt": ["tm_settings"],
                           "letsencrypt-dns": ["static_config"]},
 "default_entrypoints": ["websecure"], "default_cert_resolver": "",
 "config_files": ["_shared.yml", "media.yml"], "config_file_template": "{service}.yml",
 "source": "traefik_manager"}
```

`source` is `"unavailable"` when Traefik Manager is not configured or Traefik
behind it cannot be reached; the form then shows a clear warning and a
free-text comma field (still sent explicitly — nothing is defaulted for you).

* Entry points: `GET /api/traefik/entrypoints` on Traefik Manager, which proxies
  Traefik's own `/api/entrypoints` (or answers `502 Traefik API unreachable`,
  which reads as unavailable).
* Certificate resolvers: the **union** (de-duplicated, stable order) of every
  place one can be seen, each name tagged with the sources that report it
  (`cert_resolver_sources`; the form shows the first as a suffix - `letsencrypt-dns
  (static config)` - and all of them in a tooltip):
  1. `tm_settings` - `GET /api/settings` -> `cert_resolver`, TM's own comma list;
  2. `static_config` - `GET /api/static/config` -> `parsed.certificatesResolvers`,
     the resolvers **declared in Traefik's static configuration**. TM answers
     `{"raw", "parsed", "path"}` only when it has a static config path
     (`STATIC_CONFIG_PATH` / Settings -> static config path) and that file is
     mounted into **its** container; otherwise `404 {"error": ...}` (`500` when the
     file is not valid YAML). Same API key as every other read, no extra
     permission. "Not available" is never an error - that source just adds
     nothing. If your newly added resolver is missing, this is almost certainly
     why: give TM the static config path;
  3. `runtime_router` - `tls.certResolver` of the routers Traefik actually serves
     (`GET /api/traefik/routers`, the same read the access-log collector uses);
  4. `default_setting` - `TRAEFIK_DEFAULT_CERT_RESOLVER`, added whenever at least
     one of the three live sources could be read.

  The list is `None` (the form falls back to a free-text field and any well-formed
  name is accepted) only when **none** of those live sources is readable - a
  configured default alone proves nothing about what exists. The catalog cache is
  30 s; the form's ↻ button, **Resync** and the push status (`GET /api/push/status`)
  re-read it, and the validator (`422` on `cert_resolver`) looks again with a
  fresh read before it rejects a resolver, so one added a moment ago is accepted.
  `orchestrator-cli proxy resolvers` (and `proxy entrypoints`) print the names with
  where each was seen.

**Default selection for a new host.** `TRAEFIK_DEFAULT_ENTRYPOINTS` (comma list)
if set *and* it still exists on the live Traefik; otherwise `websecure` if it
exists; otherwise the first live entry point that is not `traefik`; otherwise
there is no default and the operator must choose. `TRAEFIK_DEFAULT_CERT_RESOLVER`
likewise pre-selects a resolver (it is itself one of the resolver sources above;
ignored when every selected entry point is plain HTTP). A create/update **API call that omits
`entry_points`** resolves them the same way — for an edit: what is stored, else
what the route has on Traefik Manager, else the default — and then validates the
result like any explicit choice.

A host created before these fields existed has none *stored*. The edit form says
so ("not stored yet — saving will set it") and pre-selects from what Traefik
Manager actually has on the route (runtime router list), else the default rule.
The list shows such a host's live entry points with "not stored yet".

### Route options — Host header, certificate, redirect, upstream TLS

Per host, stored as `ProxyHost.route_options` (YAML: `route_options:`; empty =
defaults) and set in the form's **Certificate** and **Behaviour** sections:

| Option | Default | What it does |
|---|---|---|
| `pass_host_header` | **true** | The backend receives the hostname the visitor typed. With `false` Traefik sends the *upstream's own address* as `Host`; any backend that builds redirects or absolute links from it (Proxmox, PBS, most web UIs) then sends the browser to a raw LAN IP such as `http://192.168.100.18:8007`. |
| `skip_upstream_tls_verify` | false | For an `https://` upstream with a self-signed certificate (Proxmox, PBS). Only valid with an `https://` upstream. |
| `tls_main` / `tls_sans` | none | Request **one certificate for several names** instead of one per host, e.g. `example.com` + `*.example.com`. The form offers a ready-made wildcard for each base domain Traefik Manager is configured with, or custom names. Needs a cert resolver (a wildcard needs one with a DNS challenge) and a TLS entry point. This is also how a *new* certificate gets generated: the resolver issues it on first use. |
| `http_redirect` | off (new hosts: on when the middleware exists) | Adds a companion router `<route>-redirect` on the plain-HTTP entry point (`web`, never `traefik`), with the redirect middleware, sharing the main route's service — the same pair a hand-written `proxmox-redirect` / `proxmox` setup has. Needs an HTTPS entry point selected and the middleware to exist in Traefik. |

The redirect middleware is **not created by the orchestrator** (a duplicate of
one defined in your shared config would clash). Define it once, e.g.

```yaml
http:
  middlewares:
    redirect-to-https:
      redirectScheme:
        scheme: https
```

or set `TRAEFIK_REDIRECT_MIDDLEWARE` to the name you already use. The form warns
when Traefik does not have it, and the API refuses the option (`422`,
`http_redirect`).

**HSTS** is an option of the same kind: tick it on the host (or set `"hsts": true` in its route
options) and the route carries the headers middleware named by `TRAEFIK_HSTS_MIDDLEWARE` (default
`hsts-headers`, a `headers` middleware with `stsSeconds`). It needs an HTTPS entry point and the
middleware must exist (`422` on `hsts` otherwise). Off by default. See docs/sources.md for the YAML.

Two more things the adapter now gets right on every write:

* **The upstream scheme is sent.** An `https://` upstream used to reach Traefik
  Manager without a `scheme` and was written as plain `http://`.
* **`passHostHeader` is always sent explicitly.** Traefik Manager writes
  `passHostHeader: false` whenever the form field is absent — which was every
  route this orchestrator created. Re-saving an older host (Edit → Save) fixes
  it. The maintenance-page backend service deliberately keeps `false`.

Editing keeps what is stored when the request omits `route_options`; turning
`http_redirect` off removes the companion router, deleting the host removes
both, and a failed redirect write takes the main route back so there is never a
half-made pair.

### Enabled state and Traefik Manager

Traefik Manager is where a route is really on or off, so:

* **Resync adopts Traefik Manager's state** (enabled/disabled, upstream) for
  hosts it matches - only when Traefik Manager itself was read; a `partial`
  state is left alone - and a host whose last push still matched is re-stamped,
  so adopted state counts as `in_sync`.
* The list and the edit page show `tm_state` (`enabled` / `disabled` /
  `partial`) next to the push status. A difference between that and our flag is
  a `detached` (or, for an external route, pending) host.
* The push switches every route that serves the hostname for an `external`
  host (its separate `…-redirect` router included) and the route plus its
  `-redirect` companion for one of ours - using the real ids and verifying each
  toggle.

### Validation — at edit time, before anything is stored

One validator module (`orchestrator.registry.proxy_validation`) is used by the
API, `orchestrator-cli proxy add` and the YAML loader. The API answers
`422` (`409` for a duplicate hostname) with
`{"detail": {"message": "...", "errors": {"<field>": "<message>"}}}`; the form
renders each message next to its field *and* as a toast, the CLI prints
`field: message` lines.

| Field | Rule |
|---|---|
| `hostname` | valid DNS name, optional leading `*.`; no scheme, path, port or spaces; lower-cased; not used by another orchestrator proxy host |
| `upstream` | `http://` or `https://` + host + port (friendly messages for a missing port / scheme); required for a live-managed host |
| `entry_points` | non-empty; each must exist on the live Traefik when it can be read (`Entry point "https" does not exist on this Traefik. Available: web, websecure, traefik`); the internal `traefik` entry point (API/dashboard) is refused |
| `cert_resolver` | when given and the known list is available, must be in it |
| `route_options` | `skip_upstream_tls_verify` needs an `https://` upstream; `tls_sans` needs `tls_main`; certificate names must be valid hostnames (`*.` allowed) and need a cert resolver and a TLS entry point; `http_redirect` needs an HTTPS entry point selected and the redirect middleware to exist in Traefik |
| conflict (create, or a hostname change) | refused if Traefik Manager (or any other Traefik provider) already has a router for that hostname, or a route with the name we would create — it names the existing router and suggests Resync to adopt it. A route this orchestrator did not create is never silently overwritten |

When the live entry-point list cannot be read only the *shape* is checked and
what you typed is trusted.

### Verification and rollback (at push time)

Traefik Manager accepts a router that Traefik then refuses, so after a push
writes a route the runtime router is polled (`GET /api/traefik/routers`, every
~0.5 s for up to ~5 s) until it is `enabled` with no `error` and bound to the
entry points just written (`orchestrator.adapters.proxy.verify.verify_route`,
also what the list's **Router** status and Resync use):

* **Create, router rejected** - the route just created is deleted again on
  Traefik Manager (best effort), the item reports Traefik's own text, the host
  keeps its DB row and stays `pending_create`.
* **Update, router rejected** - the new configuration is kept (the previous one
  cannot be safely guessed); the item is a failed `verify` with Traefik's
  message, the fingerprint is **not** recorded (the host stays pending) and the
  list shows the router red until fixed.
* **Cannot verify** - the runtime list is unreadable or Traefik has not loaded
  the router within the window: the item succeeds with a "could not verify ..."
  note.

The list's **Traefik** column shows the router's own verdict for every host:
`router enabled`, `router error` with Traefik's message (red), `router not
loaded` or `router unverified`.

### Fixing an already-broken route

*Redirects to the raw LAN IP* (`passHostHeader: false`, written by routes created
before route options existed): **Edit** the host and **Save** — `Pass the visitor's
Host header` is on by default and is now written explicitly.

A route that already shows `entryPoint "https" doesn't exist` (created before
this change) is fixed from the dashboard: **Edit** the host, tick the entry point
that exists (e.g. `websecure`) and the right certificate resolver, **Save**. The
stale `https` is shown ticked and flagged as not existing on this Traefik; saving
rewrites the route on Traefik Manager with explicit values, then verifies it.

### Adopting a route Traefik Manager already has

Create refuses to touch a router that is not ours. **Resync** lists such routes
under "Discovered"; its **Add** button records one with `adopt: true` - no write
to Traefik Manager at all; the route reference and entry points are taken from
the existing router. An adopted (or hand-written, or reference-less) route is
`external`: **its content is never overwritten and never deleted** - deleting
the host here leaves the route alone and a push only switches it on/off (and
applies the maintenance fallback). A route reference counts as ours when the
adapter created it (`ProxyAdapter.owns_route_ref`; Traefik Manager: the
`orchestrator-` prefix).

Switching `router_provider` away from `traefik` (or clearing the upstream) on a
host with a route this orchestrator created records a pending deletion for it.

The maintenance-fallback middleware sync (further down) now also runs at push
time, per host, instead of on every edit.

## Enabling and disabling a host

A proxy host can be switched off without deleting it: the **Enabled** switch on
`/proxy-hosts`, the bulk **Enable / Disable** actions (see
[bulk_actions.md](bulk_actions.md)), `POST /api/proxy-hosts/{id}/enabled`
`{"enabled": false}`, or `orchestrator-cli bulk proxy-hosts disable ...`.
The flag is `ProxyHost.enabled` (migration `0013_proxy_host_enabled`, default
true) and the YAML field `enabled` on a proxy host (default `true`).

**All of these change the DB only.** The change is `pending` until a push
(Save to YAML, Push now, `proxy push`); until then Traefik keeps serving the
route and the list says "Disabled here - Traefik Manager still serves the route
until you push."

**What the push does.** Traefik Manager's
`POST /api/routes/<route_id>/toggle` with `{"enable": true|false}` (same
`X-Api-Key` and `X-Requested-With: fetch` headers as every other write), by the
id `GET /api/routes` lists, followed by a read-back. Disabling *moves* the
router - and its service, unless another router still uses it - out of the
config file into Traefik Manager's own `disabled_routes` settings store;
enabling writes them back unchanged. While disabled:

* Traefik itself has no runtime router for it. The list shows `disabled` in the
  Traefik column and `route disabled` as the router verdict, never `missing` /
  `router not loaded`.
* Traefik Manager's `GET /api/routes` still lists it, with `"enabled": false`
  (`RouteInfo.enabled`), so Resync still recognises the route as ours.
* `POST /delete/<id>` also removes a disabled route.

**The redirect companion.** When `route_options.http_redirect` is on, the
`<route>-redirect` router is toggled with it: on disable the redirect goes first
(plain HTTP never redirects to a route that is gone), on enable the main route
goes first. If the second toggle fails (or its read-back does not match) the
first is undone (best effort) and the item reports the failure.

**Its probe follows.** The auto-created `traefik:<hostname>` probe (see
[Feeding Traefik's view into the service's own HEALTH verdict](#feeding-traefiks-view-into-the-services-own-health-verdict))
is switched **off** (`Probe.enabled = false`) the moment the host is disabled and
back **on** when it is enabled - immediately, as a local change (it is not part
of the push, which only writes Traefik Manager). A disabled host is not served, so
that probe would fail for no reason - and it may be `required`.

### Editing a disabled host

Allowed. Traefik Manager's `/save` keeps an edited disabled route parked when
`originalId` is its id, and the push orders the work safely: enable first (when
we want it on), update, disable **last** - re-reading the routes first and
switching off anything an update brought back to life (for example a freshly
created redirect companion). Wake on hit is a local setting and works on a
disabled host as before.

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

> Resync also busts the entry-point cache, reports each matched host's runtime
> router status (`traefik` in the response, same verdicts as the list), and lists
> each discovered route's entry points so it can be adopted.

Drift detection above is read-only and automatic (it runs on every page
load). **Resync** is the opposite in both respects: it is the one thing in
this section that writes data into the database (sourced from Traefik
Manager or, as a fallback, Traefik's own API — see "Drift detection"
above), and it only ever runs when an admin clicks the **Resync from
Traefik** button on `/proxy-hosts` (`POST /api/proxy-hosts/resync`).
Nothing changes on a schedule, on page load, or as a side effect of any
other request. This is unrelated to, and does not touch, the
maintenance-page fallback middleware further down, or the push to Traefik
Manager above — that's a write to Traefik's own config/routes, not to this
database.

**Resync's role next to the push engine:** the push goes DB -> Traefik
Manager (on Save to YAML / Push now); Resync is the opposite direction. It is the
**bulk/catch-up path**: importing routes that exist on
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
   - Traefik Manager's enabled/disabled state is adopted too (when TM itself
     was read), and a host whose last push still matched is re-stamped, so
     what Resync adopted counts as `in_sync` rather than as a local edit.
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
   DB-only for a `traefik`-provider discovered host: `adopt: true` records the
   existing route as `external` (see
   ["Adopting a route Traefik Manager already has"](#adopting-a-route-traefik-manager-already-has)).
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

**It follows the host's enabled switch.** `Probe.enabled` (migration
`0016_probe_enabled`, default true; YAML `enabled`, written only when false) is set
to the proxy host's `enabled` every time the sync runs - on create, edit,
enable/disable (single and bulk, `orchestrator-cli bulk`), Resync and a registry
reload. A disabled probe is **kept, not deleted**: its `required` flag and timeout
survive disable -> enable, and the next scan runs it again. While it is disabled
the health engine does not run it, records it in the verdict's probe results as
*skipped (disabled)*, and never counts it - not in the aggregation (even when
`required`), not in the backup gate, not in the health history strip and not in the
service badge. If every probe of a service is disabled the usual rule applies
unchanged: no required probe -> UNKNOWN. The Probes tab shows a *disabled* badge and
an **Enable / Disable** button for any probe (also for hand-written ones;
`PUT /api/registry/probes/{id}` `{"enabled": false}`,
`orchestrator-cli probe disable|enable <slug> <name>`); the probe list API and
`probe list` show it; "probes required / optional" is about `required` only. Because
the sync owns `enabled` for these probes, manually enabling the probe of a disabled
host is undone by the next sync - enable the host instead.

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
- **Disable / enable** the `ProxyHost` → its probe is switched off / on (kept,
  with its `required` / timeout settings).
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

> The shared backend's never-matched router is bound to a **real** entry point read
> from the live Traefik (the configured default, else `websecure`, else the first
> non-`traefik` one) with TLS disabled — never a blank `entryPoints`, which Traefik
> Manager would turn into `https`. If no entry point can be read, the whole
> fallback is skipped (logged, fail-open) rather than create a middleware that
> points at a missing service.

1. **One shared backend service**
   (`orchestrator-maintenance-backend`), pointing at
   `ORCHESTRATOR_PUBLIC_URL`, created once and reused by every service's
   middleware. Traefik's `errors` middleware invokes a Traefik *service*
   directly — it does not need a router to reach it — but Traefik
   Manager's `POST /save` endpoint is modeled around creating a
   router+service PAIR, with no supported way to write a bare service with
   no router.

   **Naming trap**: Traefik Manager names the router after what you send as
   `serviceName` but names the *service* `<serviceName>-service`. The
   `errors` middleware must therefore say
   `service: orchestrator-maintenance-backend-service`. A reference to a name
   that does not exist makes Traefik drop the routers in that config file —
   every host in it answers 404. The sync state now checks this too: a
   `maintenance-*` middleware whose `errors.service` is not a listed service
   shows the host as desynced ("missing or broken"), and a Save to YAML /
   Push now rewrites the middleware with the right reference.

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

Mirrors the existing auto-managed Traefik probe's lifecycle (above), but the
Traefik Manager side no longer runs on an edit: it runs **at push time**
(`registry/proxy_push.py`), per host, whenever a push created/updated/enabled
its route (a route rewrite drops the middleware, so it is re-applied). A
service re-link is part of the host's fingerprint, so it is a pending update.
For a host with no route of its own that a hand-written route serves, the
fallback is the only thing pushed (`local` state with a "maintenance page
fallback not applied yet" note). A deleted host's middleware name is removed
when its pending deletion is pushed. The meaning of each case:

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

### The rule is checked on the live route, and failures are shown

The sync state does not just trust "the push ran". Traefik Manager lists each
route's middlewares (`GET /api/routes`), and a host whose live, enabled, HTTPS
route lacks `maintenance-<service>` is marked **desynced** with the change
*"maintenance page rule missing or broken on the live route"* - for routes this
orchestrator created (`detached`), for adopted ones (`external`) and for
hand-written routes serving a host. A plain-HTTP redirect router, a disabled
route, a host that is disabled here, or a Traefik Manager that does not report
middlewares never counts.

**Push** attaches the rule by editing the route's raw YAML in Traefik Manager,
then **reads the route back** and confirms the rule is really there. If Traefik
Manager refuses (a 409 because a referenced definition is not in that file, a
shared definition that would change, invalid YAML, ...) the push result for that
host shows **Traefik Manager's own message**, the host stays desynced and the
fingerprint is not recorded - nothing is swallowed into a log line any more. (The
fail-open behaviour below still applies to the places that run this as a side
effect, such as `orchestrator-cli proxy resync-maintenance`.)

### What wakes a down service: "call" or a Start button

Per service (edit page, `service update --wake-trigger`, YAML `wake_trigger`, bulk
"Set wake trigger"), once **Wake on hit** is on for the service and its node:

* **call** (the default, and how it always behaved) — any request that reaches the
  maintenance page starts the service. Nothing to press.
* **button** — the page says the service is offline and shows a **Start** button;
  a plain request starts nothing. Use it where scrapers, uptime monitors or apps
  that poll the URL would otherwise keep waking the service. The traffic-driven
  auto-wake skips these services too.

How the button works through Traefik: Traefik answers a failed request with the
maintenance page fetched on the visitor's behalf, so the page cannot receive a form
POST. Instead it carries a signed token (HMAC of the slug and the hour, keyed with
`SESSION_SECRET`, valid about two hours); pressing the button re-requests the same
URL from the browser with that token in the `X-Orchestrator-Wake` header, which
Traefik passes on to the page, and the page starts the wake. Clients that do not run
the page's script never send it. This relies on Traefik forwarding the original
request headers to the error page; if pressing Start only reloads the same page,
that is not happening on your Traefik version.

### Fail-open, by design

Unlike Resync (an explicit, admin-watched action that surfaces a real
`503` on failure), this sync is a *side effect* of a push: an unreachable or
misconfigured Traefik Manager is caught and logged here and never fails the
push item or the Save to YAML that triggered it (editing proxy hosts in the
database never needs the gateway at all any more). An operator who
needs to know whether a particular sync actually happened should check the
audit log for the expected `middleware.*` entries, or the activity page.

### Backfilling existing proxy hosts

Because this sync only runs when a push touches a host,
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
