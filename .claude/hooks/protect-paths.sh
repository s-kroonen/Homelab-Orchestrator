#!/usr/bin/env bash
#
# Claude Code PreToolUse hook: blocks an agent's changes to guardrail files (listed in .guardrails).
# Exit 2 blocks the tool call and shows stderr to the agent. Edit tools are checked by path; shell
# commands are checked when they write and name a guardrail path. Best effort for the shell: the
# binding gate is CI (guardrails check) plus a protected branch. Needs bash, grep and sed (jq or
# python3 when present, see parse-input.sh).

# shellcheck disable=SC2034  # read by the sourced parse-input.sh
input="$(cat)"
# shellcheck source=parse-input.sh
source "$(dirname "${BASH_SOURCE[0]}")/parse-input.sh" || { echo "Blocked: .claude/hooks/parse-input.sh is missing." >&2; exit 2; }
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
list="$root/.guardrails"
[[ -f "$list" ]] || exit 0

mapfile -t patterns < <(grep -vE '^[[:space:]]*(#|$)' "$list" | tr -d '\r')
msg="is a guardrail file (.guardrails): only a human maintainer changes it. Don't work around this; describe the change you need in your handover or PR."

# --- Edit tools: "file_path" / "notebook_path" ----------------------------------------------------
while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    path="${path//\\//}"              # Windows paths: backslashes to slashes
    case "$path" in
        */.claude/settings*.json|*/.claude/hooks/*|~/.claude/*|"$HOME"/.claude/*)
            [[ "$path" == "$root"/* ]] || { echo "Blocked: $path is Claude Code configuration outside this project (user or global settings). Agents never change it." >&2; exit 2; } ;;
    esac
    rel="${path#"$root"/}"
    rel="${rel#./}"
    for p in "${patterns[@]}"; do
        if [[ "$rel" =~ $p ]]; then
            echo "Blocked: $rel $msg" >&2
            exit 2
        fi
    done
done < <(hook_paths)

# --- Shell: a writing command that names a guardrail path -----------------------------------------
cmd="$(hook_command)"
[[ -z "$cmd" ]] && exit 0

# Reading secret files from the shell (the Read-tool deny rules don't cover cat, grep, source, ...).
if grep -qE -- '(^|[[:space:];&|(])(cat|less|more|head|tail|grep|rg|awk|sed|source|\.|bat|xxd|strings|base64|cp|scp|curl|python[0-9.]*|node)[[:space:]]([^|;&]*[[:space:]/"'"'"'])?\.env(\.[A-Za-z0-9_-]+)?([[:space:]"'"'"';|&)]|$)' <<<"$cmd" \
   && ! grep -qE -- '\.env\.example' <<<"$cmd"; then
    echo "Blocked: reading .env files is not allowed for agents. Ask the owner for the value you need, or use .env.example." >&2
    exit 2
fi
# Claude Code configuration outside the project (user settings, hooks): never written by an agent.
if grep -qE -- '(~|\$HOME|/root|/home/[^/]+|/Users/[^/]+)/\.claude/' <<<"$cmd"; then
    echo "Blocked: this command touches Claude Code configuration outside the project. Agents never change it." >&2
    exit 2
fi

# Redirects to /dev/null or another stream are not writes.
scan="$(sed -E 's#[0-9]*>>?[[:space:]]*/dev/null##g; s#[0-9]*>&[0-9]+##g' <<<"$cmd")"
writes='(>|(^|[^[:alnum:]_-])(python[0-9.]*|node|perl|ruby|deno|bun|php)[[:space:]]+(-[[:alnum:]]*[ce]|--eval|-)([[:space:]]|$)|(^|[^[:alnum:]_-])(tee|rm|mv|cp|truncate|chmod|ln|install|dd|unlink)([[:space:]]|$)|(^|[^[:alnum:]_-])(sed|perl)[[:space:]][^|;&]*-[[:alnum:]]*i|(^|[^[:alnum:]_-])git[[:space:]][^|;&]*(checkout|restore|rm|mv|apply|stash|reset)([[:space:]]|$))'
grep -qE -- "$writes" <<<"$scan" || exit 0

for p in "${patterns[@]}"; do
    core="${p#^}"
    core="${core%\$}"
    # A directory pattern (trailing slash) also matches the bare directory name: rm -rf .vale
    if [[ "$core" == */ ]]; then core="${core%/}(/|[[:space:]\"';|&)]|\$)"; fi
    if grep -qE -- "(^|[[:space:]\"'=:(<>]|\./|/)${core}" <<<"$scan"; then
        echo "Blocked: this command writes to a path that $msg Reading it (cat, grep, git diff) is fine." >&2
        exit 2
    fi
done
exit 0
