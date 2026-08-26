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

Phase 1 (scaffold + state model) — this repo currently builds and runs the
skeleton: config, DB schema + Alembic baseline, adapter interfaces (with
dry-run implementations), FastAPI app with `/healthz` + `/readyz`, and the
YAML ↔ DB registry reconciler with unsaved-changes detection.

See [docs/architecture.md](docs/architecture.md) for the full design and
[docs/adding_a_service.md](docs/adding_a_service.md) for how to register a
service (just a YAML entry).

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
cp config/services.example.yaml config/services.yaml   # or write your own
alembic upgrade head
orchestrator                                           # serves on :8080
```

Then `curl http://127.0.0.1:8080/healthz` and `.../api/registry/services`.

### With Docker

```bash
cp .env.example .env
cp config/services.example.yaml config/services.yaml
docker compose up -d --build
```

The container HEALTHCHECK hits `/healthz` inside the container; SQLite lives
on the named `orchestrator_state` volume (deliberately **not** on NFS, which
would break SQLite's locking).

### Dry-run

`DRY_RUN=true` (the default in `.env.example`) forces every external adapter
(power manager, Proxmox, PBS) to a logging no-op. Safe for local dev; safe for
CI. Individual adapters can be overridden with `POWER_ADAPTER`,
`PROXMOX_ADAPTER`, `PBS_ADAPTER` for mixed testing.

---

## Tests

```bash
pytest
```

Adapters are fully mocked in tests; the DB is a per-test temp SQLite file.

---

## What's coming

Phases 2–9 land the live Proxmox + PBS adapter, MQTT client to the power
manager, health engine + backup gate, maintenance page + Traefik wiring,
scheduler + retention, dashboard + passkeys, greenlight/revert flow, and the
mailcow + ntfy notifier. See the design brief for the full plan.
