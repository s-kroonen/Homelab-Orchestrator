#!/bin/sh
# A tiny stand-in for the docker CLI, just enough for scripts/updater.sh.
# State lives in $FAKE_DOCKER_STATE (files); behaviour is scripted by the test.
S="$FAKE_DOCKER_STATE"
mkdir -p "$S/map" "$S/behavior" "$S/sha"
echo "$@" >>"$S/calls"

key() { printf '%s' "$1" | tr '/:@' '___'; }
resolve() { # ref-or-id -> id
  if [ -f "$S/map/$(key "$1")" ]; then cat "$S/map/$(key "$1")"; return; fi
  case "$1" in sha256:*) printf '%s' "$1" ;; esac
}
counter() { n=$(cat "$S/restarts" 2>/dev/null || echo 0); echo $((n + 1)) >"$S/restarts"; echo "$n"; }

case "$1" in
  inspect)
    fmt="$3"
    case "$fmt" in
      *range*Env*) echo "PATH=/usr/bin"; echo "ORCHESTRATOR_GIT_SHA=$(cat "$S/sha/$(cat "$S/container.image")" 2>/dev/null)" ;;
      '{{.Image}}') [ -f "$S/container.image" ] && cat "$S/container.image" ;;
      '{{.Config.Image}}') cat "$S/container.ref" 2>/dev/null ;;
      '{{.State.Running}}') cat "$S/container.running" 2>/dev/null || echo false ;;
      *Health.Status*) cat "$S/container.health" 2>/dev/null || echo none ;;
      '{{.RestartCount}}')
        if [ "$(cat "$S/container.behavior" 2>/dev/null)" = "flap" ]; then counter; else echo 0; fi ;;
      *project.working_dir*) echo "$FAKE_PROJECT_DIR" ;;
      *project.config_files*) echo "${FAKE_CONFIG_FILES:-$FAKE_PROJECT_DIR/docker-compose.yml}" ;;
      *compose.project*) echo "homelab" ;;
    esac
    [ -f "$S/container.image" ] || exit 1
    ;;
  image)
    # image inspect -f '{{.Id}}' REF
    id="$(resolve "$5")"
    [ -n "$id" ] && echo "$id" || exit 1
    ;;
  tag)
    id="$(resolve "$2")"
    [ -n "$id" ] || exit 1
    printf '%s' "$id" >"$S/map/$(key "$3")"
    ;;
  pull)
    [ -f "$S/pull_fail" ] && { echo "Error response from daemon: unauthorized" >&2; exit 1; }
    if [ -f "$S/after_pull/$(key "$2")" ]; then
      cp "$S/after_pull/$(key "$2")" "$S/map/$(key "$2")"
    fi
    echo "Status: Downloaded newer image for $2"
    ;;
  stop)
    echo false >"$S/container.running"
    ;;
  logs)
    echo "Traceback: the new version could not start"
    ;;
  images)
    for f in "$S"/map/orchestrator-rollback_*; do
      [ -e "$f" ] || continue
      echo "orchestrator-rollback:$(basename "$f" | sed 's/^orchestrator-rollback_//')"
    done
    ;;
  rmi)
    rm -f "$S/map/$(key "$2")"
    ;;
  compose)
    # the update's first start can be made to fail; the rollback's (--force-recreate) cannot
    case "$*" in *--force-recreate*) ;; *) [ -f "$S/compose_fail_new" ] && exit 1 ;; esac
    [ -f "$S/compose_fail_all" ] && exit 1
    echo "COMPOSE ORCHESTRATOR_IMAGE=$ORCHESTRATOR_IMAGE ARGS=$*" >>"$S/compose_calls"
    id="$(resolve "$ORCHESTRATOR_IMAGE")"
    echo "$id" >"$S/container.image"
    echo "$ORCHESTRATOR_IMAGE" >"$S/container.ref"
    echo true >"$S/container.running"
    echo 0 >"$S/restarts"
    behavior="$(cat "$S/behavior/$id" 2>/dev/null || echo healthy)"
    echo "$behavior" >"$S/container.behavior"
    case "$behavior" in unhealthy) echo unhealthy >"$S/container.health" ;; *) echo healthy >"$S/container.health" ;; esac
    ;;
esac
exit 0
