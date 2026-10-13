# Claude Code cloud session — environment setup

For debugging pipeline logic (wake, backup) and running the test suite in a
[cloud session](https://claude.ai/code) linked to this repo. **Not** for
testing against real Proxmox/PBS/MQTT — the cloud VM has no route to your LAN,
so `orchestrator-cli check` and anything live will correctly fail there.
Everything useful in this environment runs with `DRY_RUN=true`.

## Setup script: leave it empty

An environment's **Setup script** runs *before* the repo is cloned, so it has
no `pyproject.toml` to install from — `pip install -e '.[dev]'` and
`docker compose build` both fail there with the cwd stuck at `/home/user`
(confirmed against a real session; that's not a hypothetical). Python
(pip/poetry/uv/black/mypy/pytest/ruff) and Docker are already pre-installed on
the session VM regardless, so there's nothing this project needs a setup
script for. Leave the field empty.

## The actual install: a committed SessionStart hook

Project-specific setup that needs the repo present belongs in a
[SessionStart hook](https://code.claude.com/docs/en/cloud-environments#install-dependencies-with-a-sessionstart-hook)
instead, which runs *after* Claude Code launches and the clone exists. This
repo already has one:

* [`.claude/settings.json`](../.claude/settings.json) — registers the hook
* [`scripts/cloud_setup.sh`](../scripts/cloud_setup.sh) — `pip install -e
  '.[dev]'`, then a best-effort `docker compose build`

It checks `CLAUDE_CODE_REMOTE` and exits immediately outside the cloud, so a
local `claude` session isn't affected. Nothing to configure — it runs
automatically for any cloud session on this repo.

## Environment variables

Still set in the environment dialog at claude.ai/code (**Add cloud
environment**, or the gear icon on an existing one) — `.env` format, one
`KEY=value` per line. Nothing here is a secret — matches
`.env.local.example`'s dry-run/local-path shape, not `.env`'s real credentials
(which never belong in a cloud environment, since anyone using it can read
these values):

```
DRY_RUN=true
DATABASE_URL=sqlite:///./data/orchestrator.db
STATE_DIR=./data
SERVICES_YAML_PATH=./config/services.yaml
RUN_MIGRATIONS_ON_START=false
LOG_FORMAT=console
LOG_LEVEL=INFO
SESSION_SECRET=cloud-session-dev-only-not-real
WEBAUTHN_RP_ID=localhost
WEBAUTHN_ORIGIN=http://localhost:8080
```

Network access: leave it at the default **Trusted** level — that already
covers PyPI and Docker Hub, which is all the hook needs.

## What actually works there

- `pytest` — the full suite is hermetic (mocked adapters, temp SQLite per test), runs identically to local.
- `alembic upgrade head`, `orchestrator-cli service list`, `orchestrator-cli scan --all`, `orchestrator-cli backup <slug>`, `orchestrator-cli wake <slug>` — all fine under `DRY_RUN=true`; nothing touches real infra.
- `docker compose up -d` against the dry-run `.env` above, to exercise the API/dashboard end to end.
- `docker compose run --rm setup ...` (`init`, `config edit`, `scaffold`) with `--no-verify`, since there's no real Proxmox/PBS to check answers against.

## What won't work, by design

- Anything hitting real Proxmox, PBS, the MQTT broker, or the host runner socket — all private-LAN-only. `orchestrator-cli check` will report `AdapterUnreachable`, correctly.
- `docker compose run --rm setup init` without `--no-verify` — it'll try to verify against a cluster the VM can't reach.
