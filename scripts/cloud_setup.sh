#!/bin/bash
# Cloud-session-only dependency install, run as a SessionStart hook (see
# .claude/settings.json). Python and Docker are already on the cloud VM's base
# image, so this just installs the project itself.
#
# Why a SessionStart hook and not the environment's "setup script": a setup
# script runs before the repo is cloned, so it has no pyproject.toml to install
# from — pip/docker compose fail with cwd stuck at /home/user. A SessionStart
# hook runs after Claude Code launches, with the repo present, so this can
# assume $CLAUDE_PROJECT_DIR is a real checkout. See docs/cloud_session_setup.md.
#
# Runs locally too (every SessionStart hook does) — exit immediately there so
# a local `claude` session doesn't get a surprise `docker compose build`.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

pip install -e '.[dev]'

# Best-effort: nice to have the image pre-built, but never fail session start
# over it — dry-run debugging via pytest/orchestrator-cli doesn't need it.
docker compose build || true
