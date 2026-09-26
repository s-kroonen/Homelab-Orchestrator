# Migrating the gateway: NPM → Traefik

This is operations work on the **gateway VM**, not this repository — a
separate agent session, run on (or with access to) that machine, does the
actual migration. This doc is that agent's brief: copy the prompt below into
a fresh session there.

## Why this repo cares

The maintenance page (phase 5) depends on **Traefik's `errors` middleware** —
routing a request to a fallback responder when its backend is down. NPM has
no equivalent. Auto-boot-on-request doesn't work until this migration lands.

It also decides how proxy state becomes observable later (phase 7's
dashboard): the convention fixed below — one static YAML file per service
under a known directory — is what a later `infra` probe or the host runner
reads to show routing/cert status per service. Land the migration in that
shape and the dashboard work is a file-parser, not a redesign.

## Why file provider, not Docker labels

The obvious Traefik setup — Docker-label discovery — **does not fit this
homelab**. Each service is its own guest VM (see `services.yaml`), and
Traefik's Docker provider only sees containers on the *same* Docker engine it
runs on. Every backend here is a different host. The prompt below has the
agent use Traefik's **file provider** instead: one static routing file per
service, each pointing at that guest's LOCAL address — the same
`curl --resolve` / Host-header pattern already documented in
[probing_through_a_gateway.md](probing_through_a_gateway.md). That doc is
worth having the migration agent read; it explains the exact hop this proxy
sits in.

## After the migration: what changes back here

Once a service is actually served through Traefik, tell this repo about it:

```bash
orchestrator-cli proxy add <slug> --hostname media.example.com --upstream http://10.0.0.10:8096 --provider traefik
```

`service list` and `probe add`'s HTTP suggestions read `router_provider` from
this, so update it per service as each one cuts over — not all at once at the
end.

---

## The prompt

Copy everything below into the agent session running on (or against) the
gateway.

~~~
You are migrating this gateway VM's reverse proxy from Nginx Proxy Manager
(NPM) to Traefik, running under Docker Compose. No tunnel software (Pangolin
or similar) is part of this — plain Traefik, reached directly, same as NPM is
today. Do this exactly as a careful sysadmin would: discover before changing,
verify before cutting over, never leave the box without a working proxy.

## Non-negotiables

- NPM keeps running, untouched, until every single host is verified working
  through the new stack. Do not stop or remove its container, volume, or
  compose service until the final step.
- Traefik listens on different host ports than NPM while both run in
  parallel (e.g. 8080/8443), so nothing currently live is disturbed. Only the
  final per-host cutover moves 80/443 to the new stack.
- Before changing anything, back up: NPM's sqlite database and its
  letsencrypt directory (both under its data bind mount), and the full
  contents of whatever compose directory holds it. Put backups outside any
  directory you're about to modify.
- Never invent a value you can verify. If you don't know a domain's real
  upstream address or port, read it out of NPM's own config or ask rather
  than guessing.
- If anything is ambiguous or destructive and not covered by the steps below,
  stop and ask rather than assuming.

## Step 0 — Permissions, before you start

You (the agent) are most likely running as the operator's own account, not
the `gateway` service account NPM runs under. Don't switch identities or run
as root to work around that — set up the narrow access actually needed:

- **Docker group membership** on whichever account is running you
  (`sudo usermod -aG docker <you>`, re-login to pick it up) — needed to run
  and verify compose stacks. Note this is root-equivalent regardless of which
  Unix user invokes `docker`; it's the real trust decision here, not which
  username is in the shell prompt.
- **Read access to NPM's directory** without touching its ownership: an ACL,
  not a home-directory permission change —
  `setfacl -R -m u:<you>:rX /home/gateway/<npm-dir>` (add `-d` for the same
  rule to apply to files created later, if you'll be re-reading it repeatedly).
- **The new stack stays owned by the `gateway` account**, matching NPM today —
  you can run `docker compose` against files you don't own once you're in the
  `docker` group, so there's no need to `chown` anything to yourself. If a
  step must create files, create them as `gateway` (`sudo -u gateway ...`) or
  `chown` them back afterward — don't leave the new proxy's config owned by a
  personal account.
- **Location**: put the new stack under `/opt/gateway-proxy/` (or `/srv/`),
  not inside `/home/gateway/` next to NPM. Config under `/etc/traefik/` (Step 3
  below) is root-owned by convention, consistent with every other system
  service on the box — this is a good moment to leave NPM's home-directory
  placement behind rather than carry it forward.

If any of this needs a password you don't have or a decision only the
operator can make, stop and ask rather than improvising around it.

## Step 1 — Inventory the current setup

Find and read NPM's actual configuration — don't assume its shape. It's
typically a `docker-compose.yml` plus a bind-mounted `data/` directory whose
`database.sqlite` holds every proxy host, and `letsencrypt/` holds certs.
Query the sqlite file directly if the NPM UI isn't convenient:

    sqlite3 /path/to/data/database.sqlite ".tables"
    sqlite3 /path/to/data/database.sqlite "select * from proxy_host;"

Also check for: custom nginx config blocks per host (advanced tab entries —
these need an equivalent in Traefik, most often a middleware), access lists /
basic auth, forced SSL / HSTS settings, and WebSocket-enabled hosts. Produce
a plain-text table before writing any config: domain, upstream host:port,
scheme, custom config (yes/no — and what), access list (yes/no).

Report this inventory back before continuing to Step 2.

## Step 2 — Install Traefik

Fetch Traefik's own current official Docker Compose installation docs and
follow them — don't reconstruct its compose file from general knowledge,
since this is the box everything else is reached through and versions/config
shape change over time.

Configure the result to run on alternate ports (see Non-negotiables) so NPM
keeps serving 80/443 during setup and verification.

## Step 3 — One static routing file per service, using the file provider

Do NOT use Traefik's Docker-label provider — the backends this proxies are
separate VMs, not containers on this host, so label discovery would see
nothing. Use the **file provider** instead, with Traefik watching a
directory, one YAML file per service:

    /etc/traefik/dynamic/<slug>.yml

Each file is self-contained, e.g.:

```yaml
http:
  routers:
    <slug>:
      rule: "Host(`media.example.com`)"
      service: <slug>
      entryPoints: ["websecure"]
      tls: {}
  services:
    <slug>:
      loadBalancer:
        servers:
          - url: "http://10.0.0.10:8096"   # the guest's LOCAL address
```

Use the LOCAL address of each guest VM, not a hostname that resolves
outward — same reasoning as this project's own gateway probes: a probe (and
now, routing) should not depend on external DNS or NAT hairpinning to reach a
host that's actually right there. `<slug>` should be a short, stable,
lowercase-with-hyphens name for the service (matching how it's already named
if you can tell from NPM's config) — this filename convention is read by
other tooling later, so keep it consistent and don't rename a file once
picked.

Carry over anything from Step 1's "custom config" column as Traefik
middlewares in the same file (redirects, headers, basic auth, etc.) — ask if
a particular NPM "advanced" block doesn't have an obvious Traefik
equivalent rather than dropping it silently.

## Step 4 — Verify each host through Traefik, on the alternate port

For every service, before touching DNS or the 80/443 binding:

    curl -H "Host: media.example.com" https://127.0.0.1:8443/ -k -v

Confirm: correct backend responds, TLS cert is valid (or explain if using
staging ACME during testing), WebSocket upgrade works if the service needs
it, any custom auth/headers behave as they did under NPM.

## Step 5 — Cut over one host at a time

For each verified service: move its actual routing (whatever currently
directs 80/443 to NPM for that host — DNS, a firewall rule, an NPM-side
removal of just that one proxy host) so traffic reaches Traefik instead.
Re-verify the real domain works end-to-end. Only then move to the next host.
Do not batch this — one host, verify, next host.

## Step 6 — Decommission NPM

Only after every host has been individually cut over and verified: stop the
NPM container (don't delete yet — keep the backup and the stopped container
for a few days), then move Traefik onto the real 80/443.

## Report back

At the end, produce: the final per-service inventory (domain → the
`/etc/traefik/dynamic/<slug>.yml` file → verified status), anything from NPM
that didn't have a clean Traefik equivalent and how you handled it, and the
location of the NPM backup.
~~~
