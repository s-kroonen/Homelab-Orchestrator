#!/bin/sh
# Updates the orchestrator container to the newest image of its own tag, checks that the
# new version really comes up, and puts the old version (and its data) back if it does not.
#
# Runs in two ways, same code:
#   * as the `updater` sidecar from docker-compose.updater.yml (loops, waiting for a request
#     that the dashboard's System page writes into the shared state volume), or
#   * one-shot, still inside that container, from the project directory on the Docker host:
#       docker compose -f docker-compose.yml -f docker-compose.updater.yml run --rm updater --update
#     (or --rollback). The state lives in a Docker volume, so it cannot run on the bare host.
#
# It only ever acts on THE orchestrator container, and only moves it to another tag of the
# image it already runs - the request file carries no commands and no image names.
#
# Files (all under $STATE_DIR/update):
#   request        written by the dashboard: ID=, ACTION=update|rollback, BACKUP=, TAG=, BY=
#   status.json    what is happening / what happened (the dashboard reads this)
#   heartbeat      "<epoch> <script version>", touched every loop - "the updater is alive"
#   log            the run's log lines
#
# POSIX sh (busybox ash is fine). Needs: docker with the compose plugin, tar, sed, grep.

set -u

VERSION=1
STATE_DIR="${STATE_DIR:-/data}"
CONTAINER="${ORCHESTRATOR_CONTAINER:-orchestrator}"
SERVICE="${ORCHESTRATOR_SERVICE:-orchestrator}"
PROJECT_DIR="${PROJECT_DIR:-}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-180}"
SETTLE_S="${SETTLE_S:-45}"
POLL_S="${POLL_S:-2}"
KEEP_ROLLBACK_IMAGES="${KEEP_ROLLBACK_IMAGES:-3}"
ROLLBACK_REPO="orchestrator-rollback"

UPDATE_DIR="$STATE_DIR/update"
REQUEST="$UPDATE_DIR/request"
STATUS="$UPDATE_DIR/status.json"
HEARTBEAT="$UPDATE_DIR/heartbeat"
LOG="$UPDATE_DIR/log"

# -- run state (written into status.json) -----------------------------------
R_ID="" R_ACTION="" R_BY="" R_BACKUP="" R_TAG=""
S_STATE="idle" S_STEP="" S_MESSAGE="" S_REASON=""
S_FROM_IMAGE="" S_FROM_SHA="" S_TO_IMAGE="" S_TO_SHA=""
S_STARTED="" S_FINISHED="" S_STEPS="" S_ROLLED_BACK="false"
# the image reference the container was running before we touched it (repo:tag)
ORIG_REF=""

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# JSON string body: escape backslash and quote, flatten control characters.
jstr() {
  printf '%s' "$1" | tr '\n\r\t' '   ' | tr -d '[:cntrl:]' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

log() {
  line="$(now) $*"
  printf '%s\n' "$line" >&2
  [ -d "$UPDATE_DIR" ] && printf '%s\n' "$line" >>"$LOG" 2>/dev/null
  return 0
}

write_status() {
  [ -d "$UPDATE_DIR" ] || return 0
  tmp="$STATUS.tmp.$$"
  {
    printf '{"id":"%s","action":"%s","state":"%s","step":"%s","message":"%s","reason":"%s",' \
      "$(jstr "$R_ID")" "$(jstr "$R_ACTION")" "$(jstr "$S_STATE")" "$(jstr "$S_STEP")" \
      "$(jstr "$S_MESSAGE")" "$(jstr "$S_REASON")"
    printf '"by":"%s","backup":"%s","tag":"%s","rolled_back":%s,' \
      "$(jstr "$R_BY")" "$(jstr "$R_BACKUP")" "$(jstr "$R_TAG")" "$S_ROLLED_BACK"
    printf '"from_image":"%s","from_sha":"%s","to_image":"%s","to_sha":"%s",' \
      "$(jstr "$S_FROM_IMAGE")" "$(jstr "$S_FROM_SHA")" "$(jstr "$S_TO_IMAGE")" "$(jstr "$S_TO_SHA")"
    printf '"started_at":"%s","updated_at":"%s","finished_at":"%s","script_version":%s,"steps":[%s]}\n' \
      "$S_STARTED" "$(now)" "$S_FINISHED" "$VERSION" "$S_STEPS"
  } >"$tmp" 2>/dev/null && chmod 644 "$tmp" && mv -f "$tmp" "$STATUS"
}

# step NAME OK|FAIL|SKIP DETAIL - one line in the run's step list, then publish.
step() {
  S_STEP="$1"
  entry="$(printf '{"name":"%s","result":"%s","detail":"%s","at":"%s"}' \
    "$(jstr "$1")" "$(jstr "$2")" "$(jstr "${3:-}")" "$(now)")"
  if [ -n "$S_STEPS" ]; then S_STEPS="$S_STEPS,$entry"; else S_STEPS="$entry"; fi
  log "step $1: $2 ${3:-}"
  write_status
}

finish() { # finish STATE MESSAGE [REASON]
  S_STATE="$1" S_MESSAGE="$2" S_REASON="${3:-}" S_FINISHED="$(now)"
  log "$1: $2 ${3:-}"
  write_status
}

# -- docker helpers -----------------------------------------------------------

# "addr:port" the container's 8080 is published on (empty if it is not running / not published).
published_addr() { docker port "$CONTAINER" 8080 2>/dev/null | head -n 1; }
is_loopback() { case "$1" in 127.*|"[::1]"*|"::1"*) return 0 ;; esac; return 1; }

container_field() { docker inspect -f "$1" "$CONTAINER" 2>/dev/null; }
image_id_of() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null; }
sha_of_container() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null |
    sed -n 's/^ORCHESTRATOR_GIT_SHA=//p' | head -n 1
}
short() { printf '%s' "$1" | sed -e 's/^sha256://' | cut -c1-12; }

compose() {
  # Same project, same files the container was created from, so it is recreated exactly
  # as `docker compose up -d` on the host would. The image is chosen by ORCHESTRATOR_IMAGE.
  project="$(container_field '{{index .Config.Labels "com.docker.compose.project"}}')"
  dir="${PROJECT_DIR:-$(container_field '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')}"
  if [ -z "$dir" ] || [ ! -f "$dir/docker-compose.yml" ]; then
    log "cannot find docker-compose.yml in '$dir' (set PROJECT_DIR; it must be mounted at the same path)"
    return 1
  fi
  # the compose file(s) the container was created from (a base file plus overrides), as
  # the host paths compose recorded - they are mounted here at the same path
  files="$(container_field '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
  args=""
  oldifs="$IFS"
  IFS=,
  for f in $files; do
    [ -f "$f" ] && args="$args -f $f"
  done
  IFS="$oldifs"
  [ -n "$args" ] || args="-f $dir/docker-compose.yml"
  # shellcheck disable=SC2086
  docker compose ${project:+-p "$project"} --project-directory "$dir" $args \
    ${ENV_FILE_ARG:-} "$@"
}

# Wait for the (re)created container to be healthy AND stay that way. 0 = good.
verify_container() { # verify_container EXPECTED_IMAGE_ID
  want="$1"
  waited=0
  while :; do
    running="$(container_field '{{.State.Running}}')"
    health="$(container_field '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
    image="$(container_field '{{.Image}}')"
    [ "$image" = "$want" ] || { VERIFY_WHY="the container is running a different image ($(short "$image"))"; return 1; }
    [ "$running" = "true" ] || { VERIFY_WHY="the container is not running"; return 1; }
    [ "$health" = "unhealthy" ] && { VERIFY_WHY="its health check reports unhealthy"; return 1; }
    [ "$health" = "healthy" ] && break
    waited=$((waited + POLL_S))
    if [ "$waited" -ge "$HEALTH_TIMEOUT_S" ]; then
      VERIFY_WHY="it was not healthy after ${HEALTH_TIMEOUT_S}s (status: $health)"
      return 1
    fi
    sleep "$POLL_S"
  done
  # healthy once - make sure it is not a crash loop that happens to be up right now
  restarts="$(container_field '{{.RestartCount}}')"
  held=0
  while [ "$held" -lt "$SETTLE_S" ]; do
    sleep "$POLL_S"
    held=$((held + POLL_S))
    running="$(container_field '{{.State.Running}}')"
    health="$(container_field '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
    now_restarts="$(container_field '{{.RestartCount}}')"
    if [ "$running" != "true" ] || [ "$health" = "unhealthy" ] || [ "$now_restarts" != "$restarts" ]; then
      VERIFY_WHY="it did not stay up (running=$running health=$health restarts $restarts -> $now_restarts)"
      return 1
    fi
  done
  return 0
}

# Stage a restore of BACKUP so the old version starts on the data it understands. The
# orchestrator applies it at its next start, before opening the database.
stage_restore() { # stage_restore BACKUP_NAME
  name="$1"
  file="$STATE_DIR/backups/$name"
  [ -f "$file" ] || { log "backup $name not found at $file"; return 1; }
  pending="$STATE_DIR/restore-pending"
  rm -rf "$pending"
  mkdir -p "$pending" || return 1
  tar -xzf "$file" -C "$pending" orchestrator.db 2>/dev/null || { log "no database in $name"; rm -rf "$pending"; return 1; }
  has_registry=false
  if tar -xzf "$file" -C "$pending" services.yaml 2>/dev/null; then has_registry=true; fi
  printf '{"backup": "%s", "staged_at": "%s", "has_registry": %s}' "$name" "$(now)" "$has_registry" >"$pending/pending.json"
  # the orchestrator runs as the same user that owns its backups directory
  owner="$(stat -c '%u:%g' "$STATE_DIR/backups" 2>/dev/null || echo)"
  [ -n "$owner" ] && chown -R "$owner" "$pending" 2>/dev/null
  return 0
}

prune_rollback_images() {
  docker images --format '{{.Repository}}:{{.Tag}}' "$ROLLBACK_REPO" 2>/dev/null |
    grep -v ':previous$' | sort -r | tail -n +"$((KEEP_ROLLBACK_IMAGES + 1))" |
    while read -r old; do docker rmi "$old" >/dev/null 2>&1; done
  return 0
}

# -- request ----------------------------------------------------------------------

match() { printf '%s' "$2" | grep -Eq "$1"; }

read_request() {
  R_ID="" R_ACTION="" R_BY="" R_BACKUP="" R_TAG=""
  while IFS= read -r line || [ -n "$line" ]; do
    key="${line%%=*}"
    val="${line#*=}"
    case "$key" in
      ID) match '^[A-Za-z0-9-]{1,40}$' "$val" && R_ID="$val" ;;
      ACTION) case "$val" in update | rollback) R_ACTION="$val" ;; esac ;;
      BACKUP) match '^orchestrator-[0-9]{8}-[0-9]{6}(-[a-z0-9-]{1,40})?\.tar\.gz$' "$val" && R_BACKUP="$val" ;;
      TAG) match '^[A-Za-z0-9._-]{1,128}$' "$val" && R_TAG="$val" ;;
      BY) match '^[A-Za-z0-9._@-]{1,64}$' "$val" && R_BY="$val" ;;
    esac
  done <"$1"
  [ -n "$R_ID" ] || R_ID="manual-$(date -u +%s)"
  [ -n "$R_ACTION" ]
}

# -- the update ------------------------------------------------------------------

do_update() {
  S_STATE="running"
  step "inspect" "OK" "requested by ${R_BY:-unknown}"

  cur_id="$(container_field '{{.Image}}')"
  cur_ref="$(container_field '{{.Config.Image}}')"
  if [ -z "$cur_id" ]; then
    step "inspect" "FAIL" "no container named '$CONTAINER'"
    finish "failed" "The updater cannot see the orchestrator container." "no container named $CONTAINER"
    return 1
  fi
  S_FROM_IMAGE="$(short "$cur_id")"
  S_FROM_SHA="$(sha_of_container "$CONTAINER")"
  ORIG_REF="$cur_ref"
  case "$cur_ref" in
    *@sha256:*) finish "failed" "The container is pinned to a digest, so there is no newer build to move to." "$cur_ref"; return 1 ;;
  esac
  # repo[:tag] - the colon only counts as a tag separator after the last slash
  # (registry.local:5000/name has one in the host part)
  last="${cur_ref##*/}"
  case "$last" in
    *:*) tag="${last##*:}"; repo="${cur_ref%:*}" ;;
    *) tag="latest"; repo="$cur_ref" ;;
  esac
  [ -n "$R_TAG" ] && tag="$R_TAG"
  target="$repo:$tag"

  # keep a handle on what runs now, so it survives the pull moving the tag
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  docker tag "$cur_id" "$ROLLBACK_REPO:$stamp" && docker tag "$cur_id" "$ROLLBACK_REPO:previous"
  step "keep_previous" "OK" "$(short "$cur_id") kept as $ROLLBACK_REPO:$stamp"

  step "pull" "OK" "pulling $target"
  if ! out="$(docker pull "$target" 2>&1)"; then
    step "pull" "FAIL" "$(printf '%s' "$out" | tail -n 3)"
    finish "failed" "Could not download $target; nothing was changed." "$(printf '%s' "$out" | tail -n 1)"
    return 1
  fi
  new_id="$(image_id_of "$target")"
  S_TO_IMAGE="$(short "$new_id")"
  if [ "$new_id" = "$cur_id" ]; then
    step "pull" "SKIP" "already running the newest $target"
    S_TO_SHA="$S_FROM_SHA"
    finish "succeeded" "Already up to date - nothing to do."
    return 0
  fi

  bind_before="$(published_addr)"
  step "recreate" "OK" "starting $S_TO_IMAGE in place of $S_FROM_IMAGE"
  if ! ORCHESTRATOR_IMAGE="$target" compose up -d --no-deps --pull never "$SERVICE" >/dev/null 2>&1; then
    step "recreate" "FAIL" "docker compose up failed"
    rollback "$cur_id" "docker compose could not start the new version"
    return $?
  fi
  S_TO_SHA="$(sha_of_container "$CONTAINER")"

  step "verify" "OK" "waiting up to ${HEALTH_TIMEOUT_S}s for healthy, then ${SETTLE_S}s of staying up"
  if verify_container "$new_id"; then
    docker tag "$cur_id" "$ROLLBACK_REPO:previous"
    prune_rollback_images
    step "verify" "OK" "healthy and stable"
    bind_after="$(published_addr)"
    if is_loopback "$bind_after" && [ -n "$bind_before" ] && ! is_loopback "$bind_before"; then
      # Rolling back would not help (the same compose file brings it back): say so, loudly.
      step "published_port" "WARN" "the port moved from $bind_before to $bind_after - only this host can reach it now. Set ORCHESTRATOR_BIND_ADDR=0.0.0.0 in .env and run: docker compose up -d --force-recreate orchestrator"
    fi
    finish "succeeded" "Updated from ${S_FROM_SHA:-$S_FROM_IMAGE} to ${S_TO_SHA:-$S_TO_IMAGE}."
    return 0
  fi
  step "verify" "FAIL" "$VERIFY_WHY"
  docker logs --tail 40 "$CONTAINER" 2>&1 | while IFS= read -r l; do log "new version: $l"; done
  rollback "$cur_id" "$VERIFY_WHY"
}

# Put the previous image (and, when there is a backup, its data) back.
rollback() { # rollback IMAGE_ID REASON
  prev_id="$1"
  reason="$2"
  S_ROLLED_BACK="true"
  step "rollback" "OK" "going back to $(short "$prev_id") because $reason"
  docker stop "$CONTAINER" >/dev/null 2>&1
  if [ -n "$R_BACKUP" ]; then
    if stage_restore "$R_BACKUP"; then
      step "restore_data" "OK" "staged $R_BACKUP - the old version restores it as it starts"
    else
      step "restore_data" "FAIL" "could not stage $R_BACKUP; starting the old version on the current data"
    fi
  else
    step "restore_data" "SKIP" "no backup was named for this update"
  fi
  # Point the container's own reference (repo:tag) back at the old image and recreate from
  # THAT, so it carries the name it always had: the System page still follows its tag and
  # the next update pulls the right thing (rather than "orchestrator-rollback:previous").
  docker tag "$prev_id" "$ORIG_REF"
  if ! ORCHESTRATOR_IMAGE="$ORIG_REF" compose up -d --no-deps --pull never --force-recreate "$SERVICE" >/dev/null 2>&1; then
    step "rollback" "FAIL" "docker compose could not start the previous version"
    finish "failed" "The update failed AND the old version could not be started. Run 'docker compose up -d' on the host." "$reason"
    return 1
  fi
  if verify_container "$prev_id"; then
    step "rollback" "OK" "the previous version is back and healthy"
    finish "rolled_back" "The update failed and the previous version was put back." "$reason"
    return 0
  fi
  step "rollback" "FAIL" "$VERIFY_WHY"
  finish "failed" "The update failed and the previous version is not healthy either. Check 'docker logs $CONTAINER' on the host." "$reason / $VERIFY_WHY"
  return 1
}

# A deliberate "go back to the previous build" (an update that worked but is wrong).
do_rollback() {
  S_STATE="running"
  step "inspect" "OK" "rollback requested by ${R_BY:-unknown}"
  cur_id="$(container_field '{{.Image}}')"
  ORIG_REF="$(container_field '{{.Config.Image}}')"
  prev_id="$(image_id_of "$ROLLBACK_REPO:previous")"
  S_FROM_IMAGE="$(short "$cur_id")"
  S_FROM_SHA="$(sha_of_container "$CONTAINER")"
  if [ -z "$prev_id" ]; then
    finish "failed" "There is no previous build to go back to." "no $ROLLBACK_REPO:previous image on this host"
    return 1
  fi
  if [ "$prev_id" = "$cur_id" ]; then
    finish "succeeded" "Already running the previous build."
    return 0
  fi
  S_TO_IMAGE="$(short "$prev_id")"
  # the data was moved forward by the newer version; the backup taken before it is the data
  # the older one understands
  rollback "$prev_id" "a rollback was requested"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    S_STATE="rolled_back"
    S_TO_SHA="$(sha_of_container "$CONTAINER")"
    finish "rolled_back" "Went back to ${S_TO_SHA:-$S_TO_IMAGE}." "requested by ${R_BY:-unknown}"
  fi
  return "$rc"
}

# -- main ------------------------------------------------------------------------

handle_request() {
  S_STARTED="$(now)" S_FINISHED="" S_STEPS="" S_ROLLED_BACK="false" S_REASON="" S_MESSAGE=""
  S_FROM_IMAGE="" S_FROM_SHA="" S_TO_IMAGE="" S_TO_SHA="" S_STEP=""
  : >"$LOG" 2>/dev/null
  taken="$REQUEST.taken"
  mv -f "$REQUEST" "$taken" 2>/dev/null || return 0
  if ! read_request "$taken"; then
    S_STATE="failed"
    finish "failed" "The request was not understood and was ignored." "bad request file"
    rm -f "$taken"
    return 1
  fi
  rm -f "$taken"
  log "request $R_ID: $R_ACTION by ${R_BY:-unknown} backup=${R_BACKUP:-none} tag=${R_TAG:-current}"
  if [ -n "${PROJECT_DIR:-}" ] && [ -f "$PROJECT_DIR/.env" ]; then ENV_FILE_ARG="--env-file $PROJECT_DIR/.env"; fi
  case "$R_ACTION" in
    update) do_update ;;
    rollback) do_rollback ;;
  esac
}

beat() { printf '%s %s\n' "$(date -u +%s)" "$VERSION" >"$HEARTBEAT" 2>/dev/null; }

write_request() { # write_request ACTION [BACKUP] [TAG]
  mkdir -p "$UPDATE_DIR"
  {
    printf 'ID=cli-%s\n' "$(date -u +%s)"
    printf 'ACTION=%s\n' "$1"
    printf 'BY=%s\n' "${USER:-host}"
    [ -n "${2:-}" ] && printf 'BACKUP=%s\n' "$2"
    [ -n "${3:-}" ] && printf 'TAG=%s\n' "$3"
  } >"$REQUEST"
}

usage() {
  cat <<EOF
updater.sh              run as a sidecar: wait for requests from the dashboard
updater.sh --once       handle a waiting request, then exit
updater.sh --update [--backup NAME] [--tag TAG]
                        update now (from a shell on the Docker host)
updater.sh --rollback [--backup NAME]
                        go back to the previous build
EOF
}

if [ ! -d "$STATE_DIR" ]; then
  # Not inside the updater container: the state lives in a Docker volume, not on this host
  # at $STATE_DIR. Run the same script in the updater container instead.
  printf '%s\n' "$STATE_DIR does not exist here. Run this inside the updater, from the project directory:" \
    "  docker compose -f docker-compose.yml -f docker-compose.updater.yml run --rm updater ${1:---once}" >&2
  exit 2
fi
mkdir -p "$UPDATE_DIR" 2>/dev/null
# the orchestrator (not root) must be able to write the request file here
owner="$(stat -c '%u:%g' "$STATE_DIR" 2>/dev/null || true)"
[ -n "$owner" ] && chown "$owner" "$UPDATE_DIR" 2>/dev/null
case "${1:-loop}" in
  loop)
    log "updater $VERSION watching $REQUEST (container '$CONTAINER')"
    while :; do
      beat
      [ -f "$REQUEST" ] && handle_request
      sleep "$POLL_S"
    done
    ;;
  --once)
    beat
    [ -f "$REQUEST" ] && handle_request
    ;;
  --update | --rollback)
    action="update"
    [ "$1" = "--rollback" ] && action="rollback"
    shift
    backup="" tag=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --backup) backup="${2:-}"; shift 2 ;;
        --tag) tag="${2:-}"; shift 2 ;;
        *) usage; exit 2 ;;
      esac
    done
    write_request "$action" "$backup" "$tag"
    beat
    handle_request
    ;;
  -h | --help) usage ;;
  *) usage; exit 2 ;;
esac
