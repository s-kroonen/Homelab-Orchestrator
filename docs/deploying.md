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

```bash
# 1. Get the compose + example config onto the Pi (any way you like):
scp docker-compose.yml .env.example config/services.example.yaml pi@pi.local:~/orchestrator/

# 2. On the Pi:
cd ~/orchestrator
cp .env.example .env
cp config/services.example.yaml config/services.yaml
$EDITOR .env config/services.yaml          # fill in real values

# 3. Pick a pinned image tag and pull it. Public repo -> no login required.
export ORCHESTRATOR_IMAGE=ghcr.io/<your-user>/homelab-orchestrator:1.0.0
docker compose pull
docker compose up -d
docker compose logs -f
```

If the repo is **private**, log in first with a Personal Access Token that
has `read:packages`:

```bash
echo $GHCR_PAT | docker login ghcr.io -u <your-user> --password-stdin
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
export ORCHESTRATOR_IMAGE=ghcr.io/<your-user>/homelab-orchestrator:1.1.0
docker compose pull
docker compose up -d
```

The container's `RUN_MIGRATIONS_ON_START=true` handles Alembic on startup;
the pre-migration SQLite snapshot lands in `/data` (the named volume).

## Rollback

Keep the previous tag around — rollback is just re-pointing:

```bash
export ORCHESTRATOR_IMAGE=ghcr.io/<your-user>/homelab-orchestrator:1.0.0
docker compose up -d
# If a migration needs undoing:
docker compose exec orchestrator alembic downgrade -1
```

## Ansible (optional)

Because the deploy is `set env var + docker compose pull + docker compose up
-d`, a minimal Ansible role is a five-task file: template `.env`, template
`services.yaml`, `community.docker.docker_compose_v2` with `pull: always`,
and one handler that restarts on config change. Left out of this repo so it
stays orchestrator-only.

## What CI does

* [`ci.yml`](../.github/workflows/ci.yml) — every push + PR: ruff, black
  `--check`, pytest.
* [`publish.yml`](../.github/workflows/publish.yml) — pushes to `main` and
  `v*.*.*` tags (and manual dispatch): runs tests again as a gate, then
  builds + pushes the multi-arch image with proper OCI metadata.

Broken tests never yield a published image.
