# Sources

Every connection the orchestrator reads from and acts on is a **source** with its own page: a
Proxmox cluster, a Portainer, a proxy (Traefik Manager), the Traefik API, a Proxmox Backup
Server, the power manager, Home Assistant. There can be several of a kind (two clusters, two
Portainers), and each has its own page, data and settings. How a kind is built is in
[connectors.md](connectors.md); the file behind them in [connections.md](connections.md).

The alert channels (ntfy, e-mail, webhook) and the SSH settings are connections too, but not
sources: they are edited on **Notifications** (Channels card) and **System** (SSH and host
runner card).

## In the sidebar

```
Sources +    All sources
             ▾ Proxmox 2      ● cluster-a  ● cluster-b
             ▸ Portainer 2 ●              (folded: a dot when one of them has a problem)
             ● Proxy · gateway            (one of a kind: one line)
```

A kind with one source is one line, its dot the source's status. A kind with several folds; the
fold holding the page you are on is open, and the others stay as you left them (remembered in this
browser). **+** opens Add source.

## All sources (`/sources`)

Every source with its kind, status, what it has ("3 nodes · 21 guests"), how many services use it
and when it was last checked. Filter chips: *Problems* and one per kind.

* **Test all** checks every source now.
* **+ Add source**: pick what it is (machines, apps, network), then fill in its fields with
  **Test** before **Add**. The new source's page opens.
* Each row's **⋯**: Open, Test, Edit settings, Make default, Switch on / off, Remove. Removing a
  source something uses asks again and names what uses it.
* A source still configured in `.env` is tagged *.env*, with **Move into the file**.
* The page's **⋯**: export shared connections, import an export from another orchestrator.

## A source's page (`/sources/<id>`)

The header has the name, kind and status, then the address, version, response time, what it has
and **used by N services** (a menu of them). **Test** checks it now; **⋯** has Settings, Make
default, Switch on / off and Remove. Tabs (kept in the URL as `?tab=`):

| Tab | What |
|---|---|
| **Overview** | Tiles (nodes online, guests running...), what needs attention, and this source's settings at a glance |
| one per resource | Nodes, Guests, Environments, Datastores...: a list with filter chips, its own table where a kind has one |
| **Settings** | Short cards, each with its own Save and Undo: *Connection* (name, address, credentials), *Behaviour* (timeouts, refresh, "default for new services"), *Safety* (Dry run, read only, protected names), *Where it lives* (site, scope, notes). Each field of a kind says which card it is on. On the side: *Status* (last check, last change, Test now) and *Remove* |
| **Raw** | What the API has, as it returns it: pick an endpoint, read it as a table or JSON, filter, copy. Credentials are hidden (`•••`). Read only |

A switched-off source keeps its page and settings; its data tabs say it is off.

The Overview has tiles, what needs attention, small tables (a Proxmox's nodes), the source's
settings at a glance and **Recent**: the last things done to or through it, from the audit log.

## Safety, per source

* **Dry run**: this source pretends. Reads come from sample data and nothing is sent; the
  others stay live. `DRY_RUN=true` still makes every one pretend.
* **Read only** (Portainer): everything is shown, nothing is started, stopped, deployed or edited.
* **Protected** (Portainer): stacks and containers, by name. Stopping, restarting, redeploying or
  deleting one (or a container of a protected stack) asks for its name. The API answers `409`
  without `?confirm=<name>`, the CLI takes `--confirm <name>`, and the assistant is refused.
  A guarded deploy (a push, the deploy button of a linked service) is not held back by it.

## Status

Pages never wait for a source: they show its **last known status**, refreshed in the background
every minute and by **Test** / **Test all** (`connectors/status.py`). A source not checked yet reads
*not checked yet*, and is checked as soon as its page opens. In dry-run mode nothing outside is
contacted and every source reads *not checked*.

## API (admins only)

| | |
|---|---|
| `GET /api/sources` | every source, its last known status, what uses it |
| `POST /api/sources/check` | check every source now |
| `GET /api/sources/kinds` | what Add source offers, with each kind's fields |
| `GET /api/sources/{id}` | one source: its connection (secrets as set / not set), status, tabs, resources, raw endpoints |
| `POST /api/sources/{id}/test` | check it now |
| `GET /api/sources/{id}/overview` | tiles, what needs attention, facts |
| `GET /api/sources/{id}/resources/{key}` | one list: rows with `id`, the column keys, `chips`, `links`, `href` |
| `GET /api/sources/{id}/raw/{key}` | one raw endpoint, credentials redacted |

A source's own call that fails answers `502` with what went wrong; a switched-off source answers
`409`. Changing a source (settings, on / off, default, remove) goes through
`/api/connections`, which validates, encrypts, audits and applies without a restart.

## Being built

Sources replaces the Connections, Containers, Nodes & guests, Proxy hosts and Format routes pages
(gone, without a redirect). The Pull page ([pull.md](pull.md)) keeps its Proxmox import,
Addresses and Portainer adopt tabs until linking both ways lands.

## Several proxies

Each proxy host belongs to one proxy (`source_id`; an unowned one is the default proxy's). A
proxy's page lists, imports, formats and pushes its own hosts only, and every action on a host
(edit, on/off, delete, move, push) goes to the proxy that owns it. A push of everything runs once
per proxy. Pages that only know a hostname link to `/find/proxy-host?hostname=…`, which opens it
on its proxy.
