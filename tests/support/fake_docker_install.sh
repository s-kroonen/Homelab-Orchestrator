#!/bin/sh
# docker stand-in for scripts/install.sh. Logs every call; behaviour from the environment:
#   FAKE_HAS_ORCHESTRATOR=1   the orchestrator container exists
#   FAKE_UPDATER_RUNNING=1    the updater container is running
#   FAKE_OLD_IMAGE=1          the running image has no `system update-status` command
#   FAKE_IMAGE=<ref>          the running container's image reference
#   FAKE_PORT=<addr>          what `docker port orchestrator 8080` says before the update
#   FAKE_PORT_AFTER=<addr>    ... and after it (default: unchanged)
S="$FAKE_DOCKER_STATE"
mkdir -p "$S"
echo "$*" >>"$S/calls"
case "$1" in
  compose)
    case "$2" in version) echo "Docker Compose version v2.29.0"; exit 0 ;; esac
    case "$*" in *"up -d orchestrator") : >"$S/upped" ;; esac
    exit 0 ;;
  port)
    if [ -f "$S/started" ] || [ -f "$S/upped" ]; then
      [ -n "${FAKE_PORT_AFTER:-${FAKE_PORT:-}}" ] && echo "${FAKE_PORT_AFTER:-$FAKE_PORT}"
    else
      [ -n "${FAKE_PORT:-}" ] && echo "$FAKE_PORT"
    fi
    exit 0 ;;
  inspect)
    name="$(eval echo "\${$#}")"
    case "$name" in
      orchestrator)
        [ "${FAKE_HAS_ORCHESTRATOR:-}" = "1" ] || exit 1
        case "$3" in
          *Config.Image*) echo "${FAKE_IMAGE:-ghcr.io/s-kroonen/homelab-orchestrator:dev}" ;;
          *) echo "abc123" ;;
        esac ;;
      orchestrator-updater)
        [ "${FAKE_UPDATER_RUNNING:-}" = "1" ] || { echo false; exit 1; }
        case "$3" in *State.Running*) echo true ;; *) echo "def456" ;; esac ;;
      *) exit 1 ;;
    esac ;;
  exec)
    shift
    [ "$1" = "orchestrator" ] || exit 1
    case "$*" in
      *update-status*)
        [ "${FAKE_OLD_IMAGE:-}" = "1" ] && { echo "Error: No such command 'update-status'." >&2; exit 2; }
        n=$(cat "$S/status_calls" 2>/dev/null || echo 0); echo $((n + 1)) >"$S/status_calls"
        echo "updater : online"
        if [ -f "$S/started" ]; then
          echo "last run: update ${FAKE_RESULT:-succeeded} - Updated from aaa to bbb."
          echo "run id  : new-run"
        else
          echo "last run: update succeeded - Updated long ago."
          echo "run id  : old-run"
        fi ;;
      *"system update"*) : >"$S/started"; echo "Update handed to the updater." ;;
    esac ;;
esac
exit 0
