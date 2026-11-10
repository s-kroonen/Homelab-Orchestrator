# Connectors and sources

* **A connector** is one *kind* of thing the orchestrator talks to: Proxmox VE, Proxmox Backup
  Server, Portainer, Traefik Manager, the Traefik API, the power manager, Home Assistant.
  Machines (SSH), Pterodactyl, Nextcloud and TrueNAS come later.
* **A source** is one instance of a kind: a connection in `config/connections.yaml`. There can
  be several of each kind (two Proxmox clusters, two Portainers, two proxies), and each has its
  own page under **Sources** in the dashboard.

The alert channels (ntfy, e-mail, webhook) and the SSH settings are connections too, but not
sources. They live on Settings → Notifications and Settings → General.

## The standard

Every connector is a subclass of `Connector` (`orchestrator/connectors/base.py`). It declares:

| | What | Used for |
|---|---|---|
| `kind`, `title`, `group`, `family`, `blurb` | What it is | Sidebar group, Add source drawer |
| fields, secrets, check | From its kind in `connections/kinds.py` and `connections/checks.py` | Settings tab, Test |
| `summary(ctx)` | Status and one line of what it has ("3 nodes · 21 guests") | All sources, sidebar dot |
| `overview(ctx)` | Tiles, what needs attention, facts | Overview tab |
| `resources` | Its lists: columns, chips, fetch, row and bulk actions | One tab each |
| `raw` | Endpoints shown as the API returns them, credentials redacted | Raw tab |
| `links(ctx)` | What its things are in another source | "Used by", "Runs on" |

`ctx` is a `SourceCtx`. It carries:
* the connection;
* the orchestrator's settings with this source's values;
* this source's adapter;
* the whole adapter bundle, for links into other sources.

**A resource** is a list such as nodes, guests, stacks or proxy hosts. Its `fetch` returns plain
dicts, each with:
* `id` and the column keys;
* `chips`: keys of the filter chips the row matches;
* `links`: `[{label, href}]` to related things;
* `href`: the row's own page, if it has one.

The page draws it from `columns` (the generic table), unless `view` names a page module with its
own table (Portainer's stacks, Proxmox's guests, the proxy hosts).

## Several of a kind

* **Adapters:** `adapters.for_source(id)` gives any source's adapter. The default source of
  each kind is also the bundle's attribute (`adapters.proxmox`, `adapters.proxy`...), so code
  that names no source keeps working.
* **The default source** of a kind is its default connection, or else its first enabled one.
* **Ownership:**
  * Nodes and proxy hosts record their source in `source_id`.
  * Rows without an owner are claimed by the default source of their kind
    (`connectors/ownership.py`), at start and whenever connections change. A row that already
    has an owner never moves.
  * Services name a source in their controller (`instance`).

## HTTP

New connectors talk HTTP through `connectors/http.py` (`ApiClient`):
* the same timeout and TLS rules for every connector;
* the credential in one header, never logged;
* the same errors as the adapters (`adapters/errors.py`): unreachable, TLS, auth (401/403),
  request error with status and a short body.

Existing adapters move onto it as they are touched.

## Adding a connector

1. **The kind.** Add a `KindSpec` in `connections/kinds.py`: fields, which ones are secret,
   required, help, `marker`, `multi=True`. Add a read-only check in `connections/checks.py`.
   Secrets are stored encrypted with no further work ([secrets.md](secrets.md)).
2. **The client.** Use an `ApiClient` (or an adapter with a dry-run twin, when pipelines act
   through it).
3. **The connector.** Subclass `Connector`, set `kind`, `title`, `group`, `family` and `blurb`,
   then add:
   * a `summary` that says what it has;
   * `resources` with columns and chips;
   * `raw` endpoints;
   * `links` where its things live in other sources.

   Register it with `register(...)`.
4. **Actions.** Declare them on the resource. Destructive ones confirm, and the risky ones ask
   for the name to be typed.
5. **Tests.** The contract test in `tests/test_connectors.py` covers the standard. Add a test
   that two sources of the kind each list their own data.
6. **Docs.** Add a row to the table at the top of this page.

The page needs no new code unless a resource wants its own module.
