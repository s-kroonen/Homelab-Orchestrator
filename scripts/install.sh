#!/bin/sh
# Installs - or updates - the orchestrator's Docker files in one command.
#
#   mkdir -p ~/orchestrator && cd ~/orchestrator
#   curl -fsSL https://raw.githubusercontent.com/s-kroonen/homelab-orchestrator/main/scripts/install.sh | sh
#
# (Read it first if you like: it is short, and the updater it installs can drive Docker.
#  Download it, `less install.sh`, then `sh install.sh`.)
#
# It puts these next to each other in the current directory, never touching .env or config/:
#   docker-compose.yml           the orchestrator
#   docker-compose.updater.yml   the updater add-on (one-click updates with rollback)
#   scripts/updater.sh           what the updater runs
#   scripts/install.sh           this script, so  sh scripts/install.sh  updates everything again
# then starts the orchestrator and the updater.
#
# Run it again any time to update. If the updater is running, the update goes through it
# (health check, automatic rollback); the dashboard's Update button and
# `docker exec orchestrator orchestrator-cli system update` do the same without this script.
#
# A file you changed yourself is never overwritten: the new version is written next to it as
# <file>.new and you are told. A file we wrote and you did not touch is updated in place (the
# old one is kept as <file>.bak).
#
# Options:  --dir DIR      install into DIR (default: the current directory)
#           --ref REF      branch, tag or commit to take the files from (default: the one the
#                          running image follows - dev, main or vX.Y.Z - else main)
#           --repo OWNER/NAME   (default s-kroonen/homelab-orchestrator)
#           --no-updater   do not install or start the updater
#           --no-start     put the files in place and stop

set -eu

REPO="${ORCHESTRATOR_REPO:-s-kroonen/homelab-orchestrator}"
REF=""
DIR="."
WITH_UPDATER=1
START=1
POLL_S="${INSTALL_POLL_S:-3}"
POLL_MAX="${INSTALL_POLL_MAX:-300}"
FILES="docker-compose.yml docker-compose.updater.yml scripts/updater.sh scripts/install.sh"
MARKER=".orchestrator-install"

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'HELP'
Installs or updates the orchestrator's Docker files (docker-compose.yml, the updater add-on and
its script) in the current directory, then starts or updates everything.

  --dir DIR           install into DIR (default: the current directory)
  --ref REF           branch, tag or commit to take the files from
                      (default: what the running image follows - dev, main, vX.Y.Z - else main)
  --repo OWNER/NAME   default s-kroonen/homelab-orchestrator
  --no-updater        do not install or start the updater
  --no-start          put the files in place and stop
HELP
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="${2:?--dir needs a value}"; shift 2 ;;
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --repo) REPO="${2:?--repo needs a value}"; shift 2 ;;
    --no-updater) WITH_UPDATER=0; shift ;;
    --no-start) START=0; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# -- checks -------------------------------------------------------------------------------

command -v docker >/dev/null 2>&1 || die "docker is not installed (or not on PATH)"
docker compose version >/dev/null 2>&1 || die "'docker compose' is not available (install the Docker Compose plugin)"
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO "$2" "$1"; }
else
  die "need curl or wget"
fi
command -v sha256sum >/dev/null 2>&1 || die "need sha256sum"

mkdir -p "$DIR"
cd "$DIR"
HERE="$(pwd)"
say "Installing into $HERE"

# -- which version of the files ------------------------------------------------------------

image_ref() {
  ref=""
  if [ -f .env ]; then ref="$(sed -n 's/^ORCHESTRATOR_IMAGE=//p' .env | tail -n 1 | tr -d "\"' ")"; fi
  [ -n "$ref" ] || ref="$(docker inspect -f '{{.Config.Image}}' orchestrator 2>/dev/null || true)"
  printf '%s' "$ref"
}

if [ -z "$REF" ]; then
  current="$(image_ref)"
  tag=""
  case "${current##*/}" in *:*) tag="${current##*:}" ;; esac
  case "$tag" in
    dev | main) REF="$tag" ;;
    v[0-9]*.[0-9]*.[0-9]*) REF="$tag" ;;
    *) REF="main" ;;
  esac
fi
BASE="https://raw.githubusercontent.com/$REPO/$REF"
say "Taking the files from $REPO @ $REF"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

for f in $FILES; do
  mkdir -p "$TMP/$(dirname "$f")"
  fetch "$BASE/$f" "$TMP/$f" || die "could not download $BASE/$f"
  [ -s "$TMP/$f" ] || die "$BASE/$f was empty"
done

# -- put the files in place -----------------------------------------------------------------

sum() { sha256sum "$1" | cut -d' ' -f1; }
recorded() { [ -f "$MARKER" ] && awk -v f="$1" '$2 == f { print $1; exit }' "$MARKER"; return 0; }
NEW_MARKER="$TMP/marker"
: >"$NEW_MARKER"
CHANGED=""

place() {
  f="$1"
  new="$TMP/$f"
  newsum="$(sum "$new")"
  mkdir -p "$(dirname "$f")"
  if [ ! -e "$f" ]; then
    cp "$new" "$f"
    say "  installed  $f"
    CHANGED="$CHANGED $f"
    printf '%s  %s\n' "$newsum" "$f" >>"$NEW_MARKER"
    return
  fi
  cur="$(sum "$f")"
  if [ "$cur" = "$newsum" ]; then
    say "  unchanged  $f"
    printf '%s  %s\n' "$newsum" "$f" >>"$NEW_MARKER"
    return
  fi
  if [ "$cur" = "$(recorded "$f")" ]; then
    cp "$f" "$f.bak"
    cp "$new" "$f"
    say "  updated    $f   (previous version kept as $f.bak)"
    CHANGED="$CHANGED $f"
    printf '%s  %s\n' "$newsum" "$f" >>"$NEW_MARKER"
    return
  fi
  cp "$new" "$f.new"
  warn "$f has changes of your own, so it was left alone. The new version is $f.new - compare with:  diff $f $f.new"
  # keep what we last wrote on record, so a later run can tell it was still yours
  old="$(recorded "$f")"
  [ -n "$old" ] && printf '%s  %s\n' "$old" "$f" >>"$NEW_MARKER"
}

for f in $FILES; do place "$f"; done
cp "$NEW_MARKER" "$MARKER"
chmod +x scripts/updater.sh scripts/install.sh 2>/dev/null || true

[ -e .env ] || : >.env
mkdir -p config

if [ "$START" -eq 0 ]; then
  say "Files are in place; not starting anything (--no-start)."
  exit 0
fi

if [ ! -s .env ]; then
  say ""
  say ".env is empty, so this looks like a fresh install. Configure it first:"
  say "    docker compose run --rm setup init"
  say "then run this script again to start everything."
  exit 0
fi

# -- start / update ---------------------------------------------------------------------------

COMPOSE_UPDATER="docker compose -f docker-compose.yml -f docker-compose.updater.yml"
container_exists() { docker inspect -f '{{.Id}}' "$1" >/dev/null 2>&1; }
container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]; }

if [ "$WITH_UPDATER" -eq 1 ]; then
  say "Starting the updater"
  $COMPOSE_UPDATER pull updater >/dev/null 2>&1 || true
  $COMPOSE_UPDATER up -d updater
fi

# Can the running orchestrator hand an update to the updater? (An older image cannot.)
can_delegate() {
  [ "$WITH_UPDATER" -eq 1 ] || return 1
  container_exists orchestrator && container_running orchestrator-updater || return 1
  docker exec orchestrator orchestrator-cli system update-status >/dev/null 2>&1
}

status_out() { docker exec orchestrator orchestrator-cli system update-status 2>/dev/null || true; }
last_run() { status_out | sed -n 's/^last run: //p'; }
last_id() { status_out | sed -n 's/^run id *: //p'; }

if container_exists orchestrator && can_delegate; then
  say "Updating the orchestrator through the updater (health check, rolls back if it fails)"
  # the updater was just (re)started: give it a moment to report in
  tries=0
  until docker exec orchestrator orchestrator-cli system update-status 2>/dev/null | grep -q '^updater : online'; do
    tries=$((tries + 1))
    [ "$tries" -le 20 ] || die "the updater did not come online - check: docker logs orchestrator-updater"
    sleep "$POLL_S"
  done
  before="$(last_id)"
  docker exec orchestrator orchestrator-cli system update || die "the update could not be started"
  shown=""
  waited=0
  while [ "$waited" -lt "$POLL_MAX" ]; do
    sleep "$POLL_S"
    waited=$((waited + 1))
    id="$(last_id)"
    now="$(last_run)"
    # empty: the orchestrator is restarting; same id: still the previous run
    { [ -n "$id" ] && [ "$id" != "$before" ]; } || continue
    if [ "$now" != "$shown" ]; then
      say "  $now"
      shown="$now"
    fi
    case "$now" in
      *" succeeded "*) say "Done."; exit 0 ;;
      *" rolled_back "*) die "The update failed and the previous version was put back. Details: docker exec orchestrator orchestrator-cli system update-status" ;;
      *" failed "*) die "The update failed. Details: docker exec orchestrator orchestrator-cli system update-status" ;;
    esac
  done
  die "the updater has not finished after $((POLL_MAX * POLL_S))s - see the dashboard's System page"
fi

if container_exists orchestrator; then
  say "Updating the orchestrator (plain pull and restart - no rollback on this path)"
else
  say "Starting the orchestrator"
fi
docker compose pull orchestrator
docker compose up -d orchestrator
say "Done. Open the dashboard's System page; once it shows 'Updater: running', updates there roll back on failure."
