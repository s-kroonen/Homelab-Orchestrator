# Testing from a Windows PC

Short answer: **yes, all of it** — the test suite, the CLI, the API, and the
container. The orchestrator only *targets* Linux (the Pi); nothing about
developing or testing it requires one.

There are two things to get right: which env file wins, and whether your PC can
actually route to Proxmox/PBS.

---

## The env file split

`.env` is written for the container, where the app sees `/data` and
`/etc/orchestrator`. Those paths don't exist on Windows, so running natively
against `.env` alone fails with `unable to open database file`.

Rather than editing your deploy config back and forth, the app loads **two** env
files — `.env` then `.env.local`, with `.env.local` winning:

```bash
cp .env.local.example .env.local
```

The defaults in that file point at `./data` and `./config/services.yaml`, which
is all you need. Both files are git-ignored.

---

## 1. Test suite — works with no setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Every external system is mocked and the DB is a per-test temp file, so this is
hermetic — it ignores your `.env` entirely and never touches real hardware.

---

## 2. CLI and API in dry-run — no infrastructure needed

```bash
cp .env.local.example .env.local
cp config\services.example.yaml config\services.yaml
mkdir data
alembic upgrade head

orchestrator-cli service list
orchestrator-cli backup example-media
```

With `DRY_RUN=true` the adapters log what they *would* do and return plausible
values, so the whole backup pipeline runs end to end against nothing. Useful for
exercising registry changes and pipeline logic.

The API works the same way:

```bash
orchestrator
```

Then browse <http://127.0.0.1:8080/docs> for the interactive OpenAPI UI, or hit
`/healthz`, `/api/infra/status`, `/api/registry/services`.

---

## 3. Against real Proxmox / PBS — the routing caveat

This is the only part that can genuinely differ between your PC and the Pi.

Your PVE nodes sit on a different subnet from the VMs, reached via a static
route and NAT through pfSense. **The Pi has that route; your Windows PC may
not.** If it doesn't, you'll get `AdapterUnreachable` — which is a network fact,
not a bug in the orchestrator.

Check before assuming anything is wrong:

```bash
Test-NetConnection <pve-host> -Port 8006
Test-NetConnection <pbs-host> -Port 8007
```

If both succeed, real testing works from Windows:

```bash
# in .env.local
DRY_RUN=false
PROXMOX_VERIFY_TLS=false   # if PVE still has its self-signed cert
PBS_VERIFY_TLS=false
```

```bash
orchestrator-cli check
```

If they fail, either add the route on your PC, connect through the VPN, or just
run these steps from the Pi over SSH — the orchestrator behaves identically.

The error hierarchy tells you which problem you have:

| Symptom | Meaning |
|---------|---------|
| `AdapterUnreachable` | routing / firewall / DNS — the PC can't reach the host |
| `AdapterAuthError` | reached it fine; token wrong or **token ACL missing** (see [api_tokens.md](api_tokens.md)) |
| `FAIL datastore ...` | reached and authenticated; `PBS_DATASTORE` name is wrong |

---

## 4. The container, on Windows

Docker Desktop runs the same compose file:

```bash
docker compose up -d --build
docker compose logs -f
docker compose exec orchestrator orchestrator-cli check
```

Note this builds the **amd64** image for your PC. The Pi pulls **arm64**. Both
come from the same Dockerfile and the publish workflow builds both, so a working
local container is good evidence — though not proof — that the arm64 build is
fine. The CI build is the real check, and it builds both architectures.

Running the CLI *inside* the container (third command above) is the closest you
can get on Windows to what the Pi will do, since it uses the container's paths
and config rather than your local overrides.

---

## What genuinely differs on the Pi

Small list, and none of it affects application logic:

* **Architecture** — arm64 vs amd64. Covered by the multi-arch CI build.
* **Routing** — the Pi is on the management network; your PC may not be.
* **Paths** — the container uses `/data`; your local runs use `./data`. This is
  exactly what `.env.local` exists to bridge.
* **SQLite file locking** — the container writes to a Docker volume. Keep the
  DB off NFS on both.

Everything else — Python version, dependencies, adapter behaviour, the pipeline
— is identical.
