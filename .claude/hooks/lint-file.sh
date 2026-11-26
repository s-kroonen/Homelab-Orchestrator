#!/usr/bin/env bash
#
# Claude Code PostToolUse hook, started by format-file.sh: lints the file the agent just wrote or
# edited and reports problems on stderr with exit 2, so the agent sees them and fixes them. The
# edit itself stands (a PostToolUse hook cannot undo it).
#   1. LINT_FILE_CMD in check.config or project.config, with {file} for the path (empty or "none": skipped).
#   2. The writing rule (docs/process/writing.md): Vale on Markdown and source comments, through
#      `scripts/check docs-file`; skipped without Vale, out-of-scope paths are skipped by the check.
# Silent (exit 0) when the file is clean.

shopt -u patsub_replacement 2>/dev/null
# shellcheck disable=SC2034  # read by the sourced parse-input.sh
input="$(cat)"
# shellcheck source=parse-input.sh
source "$(dirname "${BASH_SOURCE[0]}")/parse-input.sh" || exit 0
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
[[ -f "$root/check.config" ]] || exit 0

cmd_tpl="$(bash -c 'cd "$1" && source ./check.config 2>/dev/null; [[ -f project.config ]] && source ./project.config 2>/dev/null; echo "${LINT_FILE_CMD:-}"' _ "$root")"
problems=""
while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    path="${path//\\//}"
    [[ "$path" == "$root"/* && -f "$path" ]] || continue
    rel="${path#"$root"/}"
    [[ "$rel" =~ ^(tests/(quality|security)-canary|spikes|\.specify|\.opengrep|node_modules|\.tools)/ ]] && continue
    if [[ -n "$cmd_tpl" && "$cmd_tpl" != none && ! "$rel" =~ ^\.claude/ ]]; then
        out="$(cd "$root" && bash -c "${cmd_tpl//\{file\}/$(printf '%q' "$rel")}" 2>&1)" \
            || problems+="lint-file: '$cmd_tpl' reports problems in $rel:"$'\n'"$(tail -n 30 <<<"$out")"$'\n'
    fi
    if [[ -x "$root/scripts/check" ]]; then
        out="$(cd "$root" && set -o pipefail && scripts/check docs-file "$rel" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')" \
            || problems+="lint-file: the writing rule (docs/process/writing.md) reports problems in $rel:"$'\n'"$(tail -n 30 <<<"$out")"$'\n'
    fi
done < <(hook_paths)

if [[ -n "$problems" ]]; then
    printf '%s' "$problems" >&2
    exit 2
fi
exit 0
