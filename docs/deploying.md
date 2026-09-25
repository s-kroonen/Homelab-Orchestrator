# Deploying to the Pi

Images are built by GitHub Actions and pushed to **GitHub Container Registry
(GHCR)** — no additional secrets to configure, `GITHUB_TOKEN` is enough.

## Image tags

The [publish workflow](../.github/workflows/publish.yml) produces these tags:

| Trigger                        | Tags produced                                                     |
| ------------------------------ | ----------------------------------------------------------------- |
| push to `main`                 | `main`, `edge`, `sha-<short>`                                     |
| push of a semver tag `v1.2.3`  | `1.2.3`, `1.2`, `1`, `latest`                                     |
| manual (`workflow_dispatch`)   | `manual-<short-sha>`                                              |

Every tag is built for **`linux/amd64`** (laptops/servers) and
**`linux/arm64`** (Raspberry Pi 4/5, 64-bit OS). Docker picks the right one
automatically when the Pi pulls.

**Deploy pinned tags, never `latest`.** The image is your control plane — a
surprise upgrade during a power event is exactly what you don't want.
`latest` exists for convenience and CI smoke tests only.

## First deploy on the Pi

Assuming the Pi already has Docker + `docker compose`:

Create the Proxmox and PBS API tokens first ([api_tokens.md](api_tokens.md)) —
the wizard checks them, it cannot create them.

```bash
# 1. The compose file is the only file the Pi needs:
scp docker-compose.yml pi@YOUR_PI_HOST:~/orchestrator/

# 2. On the Pi. Compose will not run anything until .env exists, so create an
#    empty one; init treats an empty .env as a clean install.
cd ~/orchestrator
mkdir -p config && touch .env

# 3. Pull a pinned tag and run the setup wizard. It checks each answer against
#    Proxmox and PBS as you go, then writes .env and config/services.yaml.
export ORCHESTRATOR_IMAGE=ghcr.io/s-kroonen/homelab-orchestrator:1.0.0
docker compose pull
docker compose run --rm setup init

# 4. Start it, and confirm the connection from inside the container.
docker compose up -d
docker compose logs -f
docker compose exec orchestrator orchestrator-cli check
```

The wizard asks for the image tag and writes it into `.env`, so later pulls fetch
the tag you chose. It also flags the two mistakes that are easy to miss: a
`PROXMOX_HOST` on a node that powers off, and a PBS token whose ACL exists on
only one of user and token.

### Changing the config later

| To change | Run |
|-----------|-----|
| one part of `.env` — `proxmox`, `pbs`, `network`, `power`, `web`, `runtime` | `docker compose run --rm setup config edit <section>` |
| see what is set (secrets masked) | `docker compose run --rm setup config show` |
| pull in new or moved guests | `docker compose run --rm setup scaffold` |
| add a probe to a service | `docker compose run --rm setup probe add <slug>` |

`.env` changes apply when the container is recreated: `docker compose up -d`.
Whatever a command replaces is copied aside first as `.env.bak-<time>` or
`services.bak-<time>.yaml`, both git-ignored.

**Why a separate `setup` service?** `.env` is a file only on the host; the
orchestrator container receives its values as environment variables and cannot
rewrite them. `setup` mounts the project directory so it can. It is
profile-gated, so `docker compose up` never starts it.

**Files owned by the wrong user (Linux).** The image writes as uid 1000, which
is the default user on Raspberry Pi OS. If `id -u` says otherwise, run setup as
yourself: `docker compose run --rm --user "$(id -u):$(id -g)" setup init`.

If the repo is **private**, log in first with a Personal Access Token that
has `read:packages`:

```bash
echo $GHCR_PAT | docker login ghcr.io -u s-kroonen --password-stdin
```

Pin `ORCHESTRATOR_IMAGE` in `.env` (`env_file:` picks it up) so subsequent
`docker compose pull` always fetches the tag you committed to.

## Cutting a release

Locally:

```bash
git tag -s v1.0.0 -m "release 1.0.0"     # -s uses your SSH signing key
git push origin v1.0.0
```

The publish workflow fans that one tag out to `1.0.0`, `1.0`, `1`, and
`latest`, plus SBOM + build provenance attestations.

## Updating on the Pi

```bash
export ORCHESTRATOR_IMAGE=ghcr.io/s-kroonen/homelab-orchestrator:1.1.0
docker compose pull
docker compose up -d
```

The container's `RUN_MIGRATIONS_ON_START=true` handles Alembic on startup;
the pre-migration SQLite snapshot lands in `/data` (the named volume).

## Rollback

Keep the previous tag around — rollback is just re-pointing:

```bash
export ORCHESTRATOR_IMAGE=ghcr.io/s-kroonen/homelab-orchestrator:1.0.0
docker compose up -d
# If a migration needs undoing:
docker compose exec orchestrator alembic downgrade -1
```

## Ansible (optional)

Because the deploy is `set env var + docker compose pull + docker compose up
-d`, a minimal Ansible role is a five-task file: template `.env`, template
`services.yaml`, `community.docker.docker_compose_v2` with `pull: always`,
and one handler that restarts on config change. To keep the wizard's checks,
template an answers file instead and run
`docker compose run --rm setup init --from answers.env --non-interactive --force
--include all`. Left out of this repo so it
stays orchestrator-only.

## What CI does

* [`ci.yml`](../.github/workflows/ci.yml) — every push + PR: ruff, black
  `--check`, pytest.
* [`publish.yml`](../.github/workflows/publish.yml) — pushes to `main` and
  `v*.*.*` tags (and manual dispatch): runs tests again as a gate, then
  builds + pushes the multi-arch image with proper OCI metadata.

Broken tests never yield a published image.

---

## Troubleshooting

### `Is a directory: '/etc/orchestrator/services.yaml'`

Docker created a **directory** at the mount point because the host file did not
exist when the container first started. Compose now mounts the config
*directory* rather than the single file, which avoids this — but if you already
have the stray directory, clean it up:

```bash
docker compose down
```

```bash
rmdir config/services.yaml
```

```bash
docker compose run --rm setup init
```

That writes `config/services.yaml` as a real file. **Run `init` before the first
`docker compose up`.**

### Container starts but no services are registered

Look for `registry.path.invalid` or `registry.reconcile.failed` in the logs. The
orchestrator deliberately does **not** crash on a bad registry — the maintenance
responder has to stay up to serve a fallback page — so a misconfigured deployment
looks healthy while doing nothing. The log line carries an `impact` field saying
exactly that.

Confirm what the container sees — it says in red when the file will not load, and why:

```bash
docker compose exec orchestrator orchestrator-cli service list
```

### `AdapterAuthError` against Proxmox or PBS

Almost always a missing **token** ACL rather than a wrong secret — with privilege
separation the token has its own ACL, separate from the user's. See
[api_tokens.md](api_tokens.md).

### `AdapterUnreachable`

Network, not permissions: routing, firewall, VPN, or DNS. Check from inside the
container, which is what actually matters:

```bash
docker compose exec orchestrator python -c "import socket;socket.create_connection(('YOUR_PVE_HOST',8006),5);print('ok')"
```

### The config directory is mounted read-write on purpose

The dashboard's Save writes `services.yaml` back via a `.tmp` sibling plus an
atomic rename, which needs write access to the *directory*. A `:ro` mount makes
Save permanently impossible. If you would rather the container never write your
config, add `:ro` back and accept that Save will fail — Reset and boot reconcile
still work, since those only read.
