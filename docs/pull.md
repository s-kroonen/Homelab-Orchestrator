# Pulling: what is fetched from other systems

Some of what this orchestrator shows is **not its own**: nodes and guests belong to Proxmox, routes
to Traefik Manager, stacks to Portainer, a guest's address to the guest. There is no way to create
a node or a guest here (they are made in Proxmox), so none of that is typed in or edited by hand.
It is **pulled**, on the page of the source it comes from ([sources.md](sources.md)). What only the
orchestrator knows (probes, policies, power targets, deploy settings) stays on its own pages.

| where | what | actions |
|-----|-------|---------|
| a Proxmox's **Nodes** tab | its nodes | *Import from Proxmox* (the nodes the cluster lists that are not recorded yet; each belongs to that cluster); ⋯ *Fill blank addresses and re-read the guests*, *Find the services' addresses* (guest agent / LXC, then ARP, then DHCP; an address typed in by hand is never touched) |
| a Proxmox's **Guests** tab | its guests, and the service each one is | *Link…*: to an existing service, or a new one on that guest ([Linking](sources.md#linking)) |
| a Portainer's **Stacks** tab | its stacks and what they are linked to | *Link…*: to an existing service, or adopt it as a new one with its containers, proxy hosts and probes |
| a proxy's **Import** and **Format routes** tabs | the routes its Traefik Manager has | import them as proxy hosts; format them (below) |
| a service's **Where it runs** | its address | *Find now* on its Configuration tab; *Address by hand* in the drawer |

The same actions exist as `orchestrator-cli sources pull proxmox|ips|traefik`, `orchestrator-cli
proxy format`, and as API under `/api/pull/*` (`overview`, `proxmox`, `proxmox/nodes`,
`proxmox/refresh`, `proxmox/nodes/import`, `proxmox/guests/import`, `ips`, `ips/discover`,
`traefik`, `traefik/import`, `traefik/format/plan`, `traefik/format/run`, `portainer`). The
`proxmox/nodes` ones and the `traefik` ones take `?source=<id>`: that Proxmox or that proxy. The
old Pull page (`/pull`) is gone.

## Format routes

Format is a tab of each proxy's page, **Format routes** (`/sources/<proxy>?tab=format`), and works
on that proxy's routes only. It plans as soon as it opens.

Old routes carry the syntax of whatever made them: hand-written files, an NPM migration (a "force
SSL" redirect middleware on the HTTPS router, HSTS and friends), names of their own, files nobody
manages any more. **Format** rewrites them, one by one, into the shape Traefik Manager creates for a
new host and this orchestrator creates by default, so nothing mismatches:

* a router named `orchestrator-<host>` (and its service) in the **configured config file**
  (`TRAEFIK_MANAGER_CONFIG_FILE`, default `{service}.yml`) - the files Traefik Manager edits, not the
  old hand-written ones;
* bound to the HTTPS entry point(s) with the certificate resolver;
* the visitor's Host header passed through (and *skip TLS verify* / a wildcard certificate kept
  when the route had them);
* a companion **HTTP → HTTPS redirect router** on the plain entry point, using the
  `redirect-to-https` middleware - the new way. Old redirect-like middlewares
  (`*redirect*`, `*https*`, `*ssl*`, `*force*`) are dropped because the redirect router replaces
  them; every other middleware (auth, headers, HSTS...) is **kept**, and you can untick any of them
  per host before running;
* the record is updated to match (new route reference, entry points, resolver, redirect on), so the
  push engine sees it as in sync.

The same standard is now the default for a **new** proxy host created through the API or the form
(redirect on when Traefik has the redirect middleware), so a new host and a formatted old one are the
same.

### Plan, then run

The page reads every route as Traefik Manager stores it and shows, per host: **ready**
(with exactly what changes), **already standard**, **left alone** (and why) or **skipped**. Nothing
is written. *Format the selected* then runs those hosts one at a time in the background (a pipeline
run of kind `format`: Activity, audit `proxy_host.formatted` / `proxy_host.format_failed`).

Per host:

1. the route is read again; if it **changed since the plan**, it is skipped;
2. a **snapshot** of the old route's stored YAML (and its redirect router) is written to
   `<STATE_DIR>/route-snapshots/<time>/<host>.json`;
3. the standard route is **created beside the old one** (`create_route`: it removes itself again if its
   own redirect router or file placement fails);
4. Traefik's runtime list must show it **loaded without error** (entry points as written); if the
   runtime list cannot be read at all the host fails unless you ticked *accept a route Traefik could
   not confirm*;
5. only then the old route and its redirect router are removed, and everything is read back;
6. the record is updated and the maintenance-page fallback is re-applied.

**If any step fails** the new route is removed and the old one stays (or, if the old one is gone, it
is recreated from the snapshot) and the host reports *restored*. By default the run **stops at the
first failure**; *continue after a failure* goes on to the next host.

Left alone, never touched:

* a route whose YAML says something a rewrite would lose: a rule other than a single `Host()`, a
  `priority`, TLS options, several servers or a sticky/health-checked load balancer, a shared service,
  a server transport other than skip-verify. The plan lists the reason;
* hosts that are **switched off** (they stay off, nothing is created or removed for them);
* routes with no record, parked routes and non-Traefik hosts;
* other routers that also match the host (the plan warns about them).

An already-owned route (`orchestrator-…`) in the wrong file is *moved*, and one without its redirect
router gets it (an *update*); one that matches the standard is reported as *already standard*.

### Format all

*Format all N ready route(s)* selects every ready host and runs them, one at a time, with the same
safety as above (snapshot, verify, restore, stop at the first failure unless told otherwise). It asks
once, naming the count. The same from a shell: `orchestrator-cli proxy format --yes` (add `--plan` to
only look, `--hostname` to pick, `--continue-on-error`).

### HSTS

The **HSTS** choice above the list is applied to every formatted route and re-plans when changed:

* *keep what each route has* (default): a route that already carries the HSTS middleware keeps it;
* *add to every formatted route*: attaches the middleware named by `TRAEFIK_HSTS_MIDDLEWARE`
  (default `hsts-headers`) and stores `hsts: true` in the host's route options;
* *take off every formatted route*.

If Traefik has no such middleware the orchestrator **creates it** (through Traefik Manager, when the
first route that uses it is written; the plan says "creates the … middleware"). What it creates:

```yaml
headers:
  stsSeconds: 31536000        # one year
  stsIncludeSubdomains: false # this host only
  stsPreload: false
  forceSTSHeader: false
```

Edit it in Traefik Manager if you want something else (e.g. `includeSubdomains`); an existing
middleware of that name is never touched.

CLI: `orchestrator-cli proxy format --hsts` / `--no-hsts`. New hosts get it from the **HSTS** checkbox
of the host form (off by default: browsers remember HSTS for a long time, so switch it on once
HTTPS works for sure).

### Not verified against a real Traefik Manager yet

The whole flow runs against a simulated Traefik Manager in the tests (create, runtime errors, failed
deletes, stale plans, restore). It has **not** been run against yours. Before formatting everything:
run the plan, format **one** low-stakes host with *stop at the first failure*, look at it in Traefik
Manager (its file, the redirect router, the middlewares) and at the snapshot, then do the rest. A
brief overlap of the old and the new router for the same host is expected and harmless (same upstream,
same middlewares).
