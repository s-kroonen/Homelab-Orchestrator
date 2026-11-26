#!/usr/bin/env bash
#
# Claude Code Stop hook: when the agent ends a turn with changed files, run the stages of STOP_STAGES
# (check.config, project.config; default: fast, test, build). A failure blocks the stop (exit 2) and the output goes
# back to the agent to fix. The build stage does nothing while BUILD_CMD is "none" or empty.
# Docs-only changes (*.md) are skipped. After 3 failed runs in a row the agent may stop, so a check
# it cannot fix doesn't keep it looping; the failure is then reported to the human.

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
state_dir="$(git rev-parse --git-dir)/agent-checks"
mkdir -p "$state_dir"

fingerprint="$( { git diff HEAD -- . ':!*.md'
    git ls-files -oz --exclude-standard -- . ':!*.md' | xargs -0 -r sha1sum
} | sha1sum | cut -d' ' -f1)"

[[ -f "$state_dir/passed" && "$(cat "$state_dir/passed")" == "$fingerprint" ]] && exit 0

stages="$(bash -c 'source ./check.config 2>/dev/null; [[ -f project.config ]] && source ./project.config 2>/dev/null; echo "${STOP_STAGES:-fast test}"')"
output=""; ok=true
for stage in $stages; do
    [[ "$stage" =~ ^[a-z][a-z-]*$ ]] || continue
    out="$(scripts/check "$stage" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')"
    output+="$out"$'\n'
    if [[ "$out" != *"ALL CHECKS PASSED"* || "$out" == *FAILED* ]]; then ok=false; break; fi
done
if $ok; then
    printf '%s' "$fingerprint" >"$state_dir/passed"
    rm -f "$state_dir/failures"
    exit 0
fi

count=$(( $(cat "$state_dir/failures" 2>/dev/null || echo 0) + 1 ))
if [[ $count -ge 3 ]]; then
    rm -f "$state_dir/failures"
    echo '{"systemMessage": "scripts/check failed 3 times in a row; the agent stopped without passing checks. Say so in the handover."}'
    exit 0
fi
echo "$count" >"$state_dir/failures"

{
    echo "scripts/check failed (Stop hook). Fix the cause; never weaken a test, check or guardrail."
    echo
    printf '%s\n' "$output" | tail -n 80
} >&2
exit 2
