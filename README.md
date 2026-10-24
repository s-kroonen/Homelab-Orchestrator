# homelab-orchestrator

A control-plane service for a low-power Proxmox homelab. It:

1. **Wakes heavy nodes on demand** via an existing power manager, so burst
   nodes stay off when idle.
2. **Gates backups on integrity checks** — a service is only backed up when its
   probes agree it's healthy, so a corrupt state never overwrites a good
   backup.
3. **Serves maintenance pages + triggers auto-boot** when a request lands on a
   down service (via a Traefik `errors` middleware).
4. **Provides a dashboard** with the current state of the stack and a small
   set of privileged actions (wake, backup now, greenlight a restore).

The orchestrator is a **control plane**. It is deliberately out of the request
data path — if it dies, external access to already-running services keeps
working (fail-open).

---

## Status

**Phases 1, 2, 3, 4, 5 and 7 complete.** Working today:

* Config, structured logging, DB schema + Alembic migrations
* YAML ↔ DB registry reconciler with unsaved-changes detection
* Live **Proxmox VE** and **PBS** adapters (guests, `vzdump`, task polling,
  snapshots, verify, prune, protect) plus dry-run equivalents
* **The integrity gate.** 13 probe kinds over pluggable transports — HTTP/TCP/MQTT
  from the orchestrator; commands via the **host runner** (which keeps the
  inventory, playbooks and keys off this container), direct **SSH** with
  ProxyJump, or locally. Only a HEALTHY verdict backs up; FAILED and UNKNOWN
  both refuse and leave the last known-good backup untouched.
* **`depends_on`** — a gateway outage reports as one problem naming the gateway,
  not as every service behind it breaking at once.
* A backup pipeline that runs end to end and records every step
* **The wake pipeline** — power-on via the existing power manager, poll until
  the node is online, start the guest, poll health until HEALTHY or timeout
* **The maintenance page** — `GET /maintenance/{slug}`, wired to Traefik's
  `errors` middleware, renders a live-progress "starting up" page for an
  arbitrary unauthenticated visitor and fires the wake pipeline
* **The dashboard + passkey login** — service list with health/backup status,
  Save/Reset-to-YAML, WebAuthn passkey auth (no passwords), admin invite
  links, multi-device support, and a CLI break-glass recovery path; see
  [docs/dashboard_auth.md](docs/dashboard_auth.md)
* `orchestrator-cli` — connectivity checks, health scans, manual backups, and
  guided builders for the whole config
* **Bulk actions** — tick services, nodes, proxy hosts or guests and enable,
  disable, wake, back up, power off, start/stop them in one go, with a per-item
  result (also `orchestrator-cli bulk`); proxy hosts can be disabled without
  being deleted. See [docs/bulk_actions.md](docs/bulk_actions.md)
* **Deferred push** — the database is the working copy; edits to proxy hosts
  (add, edit, enable/disable, delete) never touch Traefik Manager. **Save to
  YAML** writes `services.yaml` and then pushes every pending change, **Push
  now** pushes without saving, and a per-host status (pending / detached /
  external / in sync) shows what is not pushed or was changed behind your back
  (`orchestrator-cli proxy status` / `proxy push`). Other outside services can
  plug into the same engine. See [docs/proxy_hosts.md](docs/proxy_hosts.md#pushing-to-traefik-manager)
* **Config files and certificate resolvers** — new proxy routes are created in
  one Traefik Manager config file per service (`<service>.yml`, template
  `TRAEFIK_MANAGER_CONFIG_FILE`, per-host override; routes that already exist stay
  put and can be moved on request), and the certificate resolver list is the union
  of Traefik Manager's setting, Traefik's static config, the resolvers routers use
  and the configured default. A disabled proxy host switches its auto-created probe
  off (probes can be disabled by hand too) so it no longer affects the verdict. See
  [docs/proxy_hosts.md](docs/proxy_hosts.md#config-files-where-a-route-lives)
* **Service IPs** — each service has an address: a manual `ip_address`, or one
  discovered from Proxmox (QEMU guest agent / LXC) and, by the NIC MACs, from
  an ARP table or DHCP leases. Proxy hosts can use it as their upstream ("Use
  service IP") and a `ping` probe pings it. See
  [docs/service_ips.md](docs/service_ips.md)
* FastAPI app: `/healthz`, `/readyz`, `/wake`, `/maintenance`, the dashboard,
  registry, infra, and backup endpoints (the latter four behind login)

**Worth knowing:** a service with no *required* probe reports UNKNOWN and is
refused. That is deliberate — unverified is not the same as healthy — but it
means a freshly scaffolded service will not back up until you add a probe.

* **Scheduled backups + retention** — every backup policy with a
  `schedule_cron` runs on that schedule (APScheduler, `SCHEDULER_ENABLED`),
  through the same gated pipeline as a manual backup; after each successful
  run the policy's `keep_*` counts are applied via PBS prune and, with
  `protect_latest_verified`, the newest verified snapshot is pinned first.
  Policies are editable on the dashboard's **Backup policies** page and
  saved to services.yaml with the usual Save button.

**Not yet built:** greenlight/revert (phase 8), notifier + Grafana metrics
(phase 9). See [docs/roadmap.md](docs/roadmap.md) for the fuller backlog.

See [docs/architecture.md](docs/architecture.md) for the full design,
[docs/adding_a_service.md](docs/adding_a_service.md) for registering a service,
[docs/backup_wake.md](docs/backup_wake.md) for waking storage and the services
being backed up before a backup (prerequisites, holds, failure semantics),
[docs/api_tokens.md](docs/api_tokens.md) for the API token privileges,
[docs/dashboard_auth.md](docs/dashboard_auth.md) for the passkey/invite/session
mechanics behind the dashboard,
[docs/probing_through_a_gateway.md](docs/probing_through_a_gateway.md) for
probing services the orchestrator cannot reach directly,
[docs/host_runner.md](docs/host_runner.md) for running credentialed checks
off-container,
[docs/service_tree.md](docs/service_tree.md) for sub-services, the tree and the map,
[docs/portainer.md](docs/portainer.md) for reading Portainer stacks, containers and logs,
[docs/pwa_push.md](docs/pwa_push.md) for installing the app and push notifications,
[docs/system.md](docs/system.md) for backups, restore, schema upgrades,
updating (one command or one click, with automatic rollback if the new version does not come up) and
restarting the orchestrator from the dashboard,
[docs/service_ips.md](docs/service_ips.md) for how service IPs are found
(guest agent, LXC, ARP, DHCP) and the ping probe, and
[docs/proxmox_connectivity.md](docs/proxmox_connectivity.md) for the single
entry point / quorum / failover notes, and
[docs/gateway_traefik_migration.md](docs/gateway_traefik_migration.md) for
moving the gateway from NPM to Traefik (a prerequisite for the
maintenance page's auto-boot-on-request), and
[docs/cloud_session_setup.md](docs/cloud_session_setup.md) for the
environment setup script for a claude.ai/code cloud session on this repo.

---

## Repository hygiene

**This repo is public.** No real hostnames, IPs, VMIDs, tokens, keys, or paths
belong in the tree. Live configuration lives outside the repo:

* [.env.example](.env.example) is committed with placeholders — copy to `.env`
  (git-ignored) and fill in real values there.
* [config/services.example.yaml](config/services.example.yaml) uses fake
  service names — copy to `config/services.yaml` (git-ignored) and describe
  your real services there.

---

## YAML is the *saved* config; the DB is *live*

* On boot, the orchestrator overwrites its registry tables from
  `services.yaml`. Runtime tables (state, runs, backups, audit, approvals)
  and admin/passkey tables are untouched.
* Dashboard edits are written to the DB and take effect immediately.
* The dashboard shows an **unsaved changes** badge when the DB differs from
  `services.yaml`.
  * **Save** — write the DB back to `services.yaml`.
  * **Reset to saved** — re-run the boot reconcile, discarding live edits.

Verify the round-trip via `pytest tests/test_registry_loader.py`.

---

## Quick start (dev, dry-run)

```bash
python -m venv .venv && source .venv/bin/activate   # or `.venv\Scripts\activate` on Windows
pip install -e '.[dev]'
cp .env.example .env
cp .env.local.example .env.local                       # local paths; overrides .env
cp config/services.example.yaml config/services.yaml   # or write your own
alembic upgrade head
orchestrator                                           # serves on :8080
```

Then `curl http://127.0.0.1:8080/healthz`, or browse the OpenAPI UI at
<http://127.0.0.1:8080/docs>. The dashboard itself is at
<http://127.0.0.1:8080/> — the first visit registers a first-admin passkey,
no invite needed (see [docs/dashboard_auth.md](docs/dashboard_auth.md)); every
`/api/registry/*`, `/api/backup/*`, and `/api/infra/*` endpoint needs that
login too.

**Two env files, later wins.** `.env` holds deployment config (container paths
like `/data`); `.env.local` is an optional developer override for running
natively, where those paths don't exist. Both are git-ignored. Full walkthrough
including Windows: [docs/local_testing.md](docs/local_testing.md).

### With Docker (local build)

```bash
mkdir -p config && touch .env            # compose will not run anything until .env exists
docker compose build
docker compose run --rm setup init       # asks, checks against the cluster, writes .env + config/
docker compose up -d
```

The container HEALTHCHECK hits `/healthz` inside the container; SQLite lives
on the named `orchestrator_state` volume (deliberately **not** on NFS, which
would break SQLite's locking).

### On the Pi (pull from GHCR)

Images are built by [GitHub Actions](.github/workflows/publish.yml) for both
`linux/amd64` and `linux/arm64` and pushed to GHCR. On the Pi, pin a tag and
pull:

```bash
export ORCHESTRATOR_IMAGE=ghcr.io/s-kroonen/homelab-orchestrator:1.0.0
docker compose pull
docker compose up -d
```

Full deploy / update / rollback flow: [docs/deploying.md](docs/deploying.md).

### Proxmox / PBS API tokens

The orchestrator needs a scoped PVE token and a scoped PBS token. Exact
privileges, the `pveum` / `proxmox-backup-manager` commands to create them,
and the privilege-separation gotcha that causes most 403s are documented in
[docs/api_tokens.md](docs/api_tokens.md).

### Dry-run

`DRY_RUN=true` (the default in `.env.example`) forces every external adapter
(power manager, Proxmox, PBS) to a logging no-op. Safe for local dev; safe for
CI. Individual adapters can be overridden with `POWER_ADAPTER`,
`PROXMOX_ADAPTER`, `PBS_ADAPTER`, `PROXY_ADAPTER` for mixed testing.

---

## The CLI

`orchestrator-cli` is a first-class operational tool, not just a test harness —
it is the break-glass path when the dashboard is unavailable, and it works
without HTTPS, a browser, or a registered passkey.

```bash
orchestrator-cli check                  # can I reach Proxmox and PBS?
orchestrator-cli guests                 # what does Proxmox see?
orchestrator-cli snapshots              # what is in the PBS datastore?
orchestrator-cli scan <slug> | --all    # what will the backup gate decide?
orchestrator-cli backup <slug>          # wake what it needs, then run one backup end to end
orchestrator-cli backup plan <slug>     # preview what that backup would wake (exit 1 on a blocker)
```

Setting up and changing the config is three commands, each with one job:

| Command | Writes | Use it for |
|---------|--------|------------|
| `init` | `.env` **and** `services.yaml` | a clean install — replaces both |
| `config show` / `config edit <section>` | `.env` only | changing one part: `proxmox`, `pbs`, `network`, `power`, `web`, `runtime` |
| `scaffold` | `services.yaml` only | pulling in new guests, moved guests and new nodes |

Every value is checked against the live cluster as it is entered, nothing is
written until the end, and a file that gets replaced is copied aside first
(`.env.bak-<time>`, git-ignored). In Docker these run through the `setup`
service: `docker compose run --rm setup init`. Single entries have their own
commands:

```bash
orchestrator-cli service list           # what would be backed up, and what blocks it
orchestrator-cli probe add <slug>       # guided probe builder
orchestrator-cli proxy add <slug>       # record a Traefik / Pangolin route
orchestrator-cli proxy status           # what is not pushed to Traefik Manager yet
orchestrator-cli proxy push --yes       # push the live DB's proxy hosts now
orchestrator-cli proxy resolvers        # certificate resolvers and where each was seen
orchestrator-cli probe disable <slug> <name>   # switch a probe off (kept, never run)
orchestrator-cli transport check        # is the host runner / ssh plumbing usable?
```

These edit `services.yaml` in place, preserving comments, and validate before
writing. All accept `--non-interactive` plus flags for scripting. See
[docs/adding_a_service.md](docs/adding_a_service.md).

Every command honours `DRY_RUN`. `check` exits non-zero on failure, so it works
as a post-deploy smoke test in Ansible or CI.

It runs **in-process**: it builds its own adapters and opens its own DB session
rather than talking to a running orchestrator. Prefer running it inside the
container so it uses exactly the config the service uses:

```bash
docker compose exec orchestrator orchestrator-cli check
```

---

## Tests

```bash
pytest
```

Adapters are fully mocked in tests; the DB is a per-test temp SQLite file.

---

## What's coming

Phases 8 and 9 land the greenlight/revert flow and the mailcow + ntfy
notifier. See the design brief for the full plan.
