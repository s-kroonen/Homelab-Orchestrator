# shellcheck shell=bash disable=SC2154
# Sourced by the Claude Code hooks (not run by itself). The caller sets $input to the hook's JSON.
#   hook_command   the shell command of a Bash tool call (tool_input.command)
#   hook_paths     the file paths of an edit tool call (file_path, notebook_path), one per line
# Parser: jq when present, else python3, else grep/sed. HOOK_PARSER=jq|python|sed forces one
# (scripts/check hooks runs every case with each parser). All three decode the JSON string escapes
# (\" \\ \n \t), so a command with quotes or backslashes reaches the rules as the shell sees it.

_hook_parser() {
    case "${HOOK_PARSER:-}" in jq|python|sed) echo "$HOOK_PARSER"; return ;; esac
    if command -v jq >/dev/null 2>&1; then echo jq
    elif command -v python3 >/dev/null 2>&1 && python3 -I -c 'import json' >/dev/null 2>&1; then echo python
    else echo sed; fi
}

_hook_unescape() {   # JSON string body on stdin -> text (sed parser only)
    sed -E 's/\\\\/\x01/g; s/\\"/"/g; s/\\n/\n/g; s/\\t/\t/g; s/\\\//\//g; s/\x01/\\/g'
}

hook_command() {
    case "$(_hook_parser)" in
        jq)     printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null ;;
        python) printf '%s' "$input" | python3 -I -c 'import json, sys
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("command") or "", end="")
except Exception:
    pass' 2>/dev/null ;;
        *)      printf '%s' "$input" \
                    | grep -oE '"command"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"' | head -n 1 \
                    | sed -E 's/^"command"[[:space:]]*:[[:space:]]*"//; s/"$//' | _hook_unescape ;;
    esac
}

hook_paths() {
    case "$(_hook_parser)" in
        jq)     printf '%s' "$input" | jq -r '.tool_input | (.file_path, .notebook_path) | select(. != null and . != "")' 2>/dev/null ;;
        python) printf '%s' "$input" | python3 -I -c 'import json, sys
try:
    t = json.load(sys.stdin).get("tool_input", {})
    for k in ("file_path", "notebook_path"):
        if t.get(k):
            print(t[k])
except Exception:
    pass' 2>/dev/null ;;
        *)      printf '%s' "$input" \
                    | grep -oE '"(file_path|notebook_path)"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"' \
                    | sed -E 's/^"[a-z_]+"[[:space:]]*:[[:space:]]*"//; s/"$//' | _hook_unescape ;;
    esac
}
