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

**Phase 2 complete.** Working today:

* Config, structured logging, DB schema + Alembic baseline
* YAML ↔ DB registry reconciler with unsaved-changes detection
* Live **Proxmox VE** and **PBS** adapters (guests, `vzdump`, task polling,
  snapshots, verify, prune, protect) plus dry-run equivalents
* A backup pipeline that runs end to end and records every step
* `orchestrator-cli` for connectivity checks and manual backups
* FastAPI app: `/healthz`, `/readyz`, registry, infra, and backup endpoints

**Not yet built:** the integrity gate (phase 4). Backups currently run
**ungated** — every run records `integrity_gate: skipped` and logs a warning
rather than hiding the gap.

See [docs/architecture.md](docs/architecture.md) for the full design,
[docs/adding_a_service.md](docs/adding_a_service.md) for registering a service,
[docs/api_tokens.md](docs/api_tokens.md) for the API token privileges, and
[docs/proxmox_connectivity.md](docs/proxmox_connectivity.md) for the single
entry point / quorum / failover notes.

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

Then `curl http://127.0.0.1:8080/healthz` and `.../api/registry/services`, or
browse the OpenAPI UI at <http://127.0.0.1:8080/docs>.

**Two env files, later wins.** `.env` holds deployment config (container paths
like `/data`); `.env.local` is an optional developer override for running
natively, where those paths don't exist. Both are git-ignored. Full walkthrough
including Windows: [docs/local_testing.md](docs/local_testing.md).

### With Docker (local build)

```bash
cp .env.example .env
cp config/services.example.yaml config/services.yaml
docker compose up -d --build
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
`PROXMOX_ADAPTER`, `PBS_ADAPTER` for mixed testing.

---

## The CLI

`orchestrator-cli` is a first-class operational tool, not just a test harness —
it is the break-glass path when the dashboard is unavailable, and it works
without HTTPS, a browser, or a registered passkey.

```bash
orchestrator-cli check                  # can I reach Proxmox and PBS?
orchestrator-cli guests                 # what does Proxmox see?
orchestrator-cli snapshots              # what is in the PBS datastore?
orchestrator-cli services               # what is in my registry?
orchestrator-cli backup <slug>          # run one backup end to end
```

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

Phases 3–9 land the MQTT client to the power manager, the health engine +
backup gate, the maintenance page + Traefik wiring, the scheduler + retention,
the dashboard + passkeys, the greenlight/revert flow, and the mailcow + ntfy
notifier. See the design brief for the full plan.
