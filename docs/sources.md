# Data sources: what is pulled from other systems

Some of what this orchestrator shows is **not its own**: nodes and guests belong to Proxmox, routes
to Traefik Manager, stacks to Portainer, a guest's address to the guest. There is no way to create
a node or a guest here (they are made in Proxmox), so none of that is typed in or edited by hand.
It is **pulled**.

The **Data sources** page (`/sources`, in the navigation under *Orchestrator*) is where that
happens: a tab per source, each listing what was fetched and holding the actions that refresh or
import it. What only the orchestrator knows (probes, policies, power targets, deploy settings) stays
on its own pages.

| tab | shows | actions |
|-----|-------|---------|
| **Proxmox** | nodes (state, address, power target) and guests (and which service has each) | *Pull from Proxmox* (fill blank node addresses, check every node, re-read every guest, scan the services on them); *Import* a node or a guest the orchestrator does not know yet |
| **Addresses** | every service with a guest: effective address, where it came from, manual vs discovered, last result | *Discover all* / *only those without an address* (guest agent / LXC, then ARP, then DHCP; a stopped guest is skipped; an address typed in by hand is never touched) |
| **Traefik Manager** | every route (file, shape: standard / hand-written, on or parked) next to its record; records with no route yet | *Import from Traefik Manager* (adopt routes that have no record, adopt upstream and on/off state); **Format routes** |
| **Portainer** | stacks, status, what they are linked to | *Pull stacks*; *Adopt…* a stack (and its containers) as sub-services, with proxy hosts, probes and deploy settings |

The same actions exist as `orchestrator-cli sources pull proxmox|ips|traefik`, `orchestrator-cli
proxy format`, and as API under `/api/sources/*` (`overview`, `proxmox`, `proxmox/refresh`,
`proxmox/nodes/import`, `proxmox/guests/import`, `ips`, `ips/discover`, `traefik`,
`traefik/import`, `traefik/format/plan`, `traefik/format/run`, `portainer`). The import buttons that
used to be on the dashboard (*Fill addresses*, *Add node*, per-node *Resync guests*), the Proxy hosts
page (*Resync from Traefik*, *Discovered*) and the Portainer page (*Adopt*) moved here; those pages
link to it. Browsing containers and logs, and creating or deleting a stack, stay on the Portainer
page.

## Format routes

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

*Plan a format…* reads every route as Traefik Manager stores it and shows, per host: **ready**
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

The middleware has to exist in Traefik (the plan warns and adds nothing if not), e.g. in a shared file:

```yaml
http:
  middlewares:
    hsts-headers:
      headers:
        stsSeconds: 31536000
        stsIncludeSubdomains: true
        forceSTSHeader: true
```

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
