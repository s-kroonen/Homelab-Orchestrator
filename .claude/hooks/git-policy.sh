#!/usr/bin/env bash
#
# Claude Code PreToolUse hook: git rules per work type (AGENTS.md, "Work types and AI levels").
#   The agent may create, commit on and push only these branches:
#     - agentic work types and spikes: feat/, fix/, release/, spike/
#     - branches made by Claude Code on the web / the Claude GitHub Action (claude/, ccr-): those
#       sessions are agentic by nature; CI and branch rules gate what they push.
#   Everything else: the agent leaves changes uncommitted; a person commits.
# Never allowed for the agent: merge, rebase, tag, history rewrites, reset --hard, clean, force
# push, pushing to main/dev, --no-verify, changing git hook paths. Indirect forms (git in a
# variable, from a script interpreter) are treated as the real thing. Best effort: CI and branch
# rules are the binding gate. Exit 2 blocks and tells the agent why.

# shellcheck disable=SC2034  # read by the sourced parse-input.sh
input="$(cat)"
# shellcheck source=parse-input.sh
source "$(dirname "${BASH_SOURCE[0]}")/parse-input.sh" || { echo "Blocked: .claude/hooks/parse-input.sh is missing." >&2; exit 2; }
cmd="$(hook_command)"
[[ -z "$cmd" ]] && exit 0

AGENT_BRANCHES='^(feat|fix|release|spike)/|^claude/|^ccr-'
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
branch="$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"

block() { echo "Blocked by .claude/hooks/git-policy.sh: $1 See AGENTS.md, 'Work types and AI levels'." >&2; exit 2; }
has() { grep -qE -- "$1" <<<"$cmd"; }

mentions_git=false
has '(^|[^[:alnum:]_.-])git([^[:alnum:]_-]|$)' && mentions_git=true
$mentions_git || exit 0

# Plain read-only git commands are always fine.
if [[ "$cmd" =~ ^[[:space:]]*git[[:space:]]+(log|show|diff|status|blame|branch[[:space:]]+--show-current|rev-parse|ls-files|describe)([[:space:]]|$) ]] \
   && ! [[ "$cmd" =~ [\;\&\|\`] || "$cmd" == *'$('* ]]; then
    exit 0
fi

# A subcommand in git position: after `git [-C dir]`, after a variable holding git, or as a
# subprocess argument ("git", "merge"). Words inside a commit message don't count.
sub='(merge|rebase|tag|filter-branch|filter-repo|cherry-pick)'
has "(^|[^[:alnum:]_.-])git[[:space:]]+(-C[[:space:]]+[^[:space:]]+[[:space:]]+)?${sub}([[:space:]]|$)|\\\$\\{?[A-Za-z_][A-Za-z0-9_]*\\}?[[:space:]]+${sub}([[:space:]]|$)|\\\\*[\"']git\\\\*[\"'][[:space:]]*,[[:space:]]*\\\\*[\"']${sub}\\\\*[\"']" \
    && block "merge, rebase, tag and history rewrites are for a person."
has 'reset[[:space:]]+[^|;&]*--hard|(^|[^[:alnum:]_-])clean[[:space:]]+-[[:alnum:]]*f' && block "reset --hard and clean -f throw work away."
has '--no-verify|core\.hooksPath|-c[[:space:]]+core\.|GIT_CONFIG_(COUNT|KEY|PARAMETERS)' && block "git hooks must run (no --no-verify, no hook-path or config overrides)."
has 'push[[:space:]][^|;&]*(--force|[[:space:]]-f([[:space:]]|$)|--force-with-lease|--mirror|--delete|[[:space:]]:)' && block "no force pushes, mirror pushes or remote branch deletes."
has 'push[[:space:]][^|;&]*([[:space:]:/]|^)(main|dev)([[:space:]]|$)' && block "main and dev only change through a reviewed merge."

# Creating a branch: only agent branches.
if [[ "$cmd" =~ (switch[[:space:]]+(-c|-C|--create)|checkout[[:space:]]+(-b|-B)|branch)[[:space:]]+([^[:space:]\;\&\|\"\']+) ]]; then
    new="${BASH_REMATCH[4]}"
    [[ "$new" =~ ^- ]] || [[ "$new" =~ $AGENT_BRANCHES ]] \
        || block "the agent only creates branches matching ${AGENT_BRANCHES}; '$new' is for a person to create."
fi

# Commit / push (also via a variable, a subprocess or an interpreter): only on an agent branch.
if has '(^|[^[:alnum:]_-])(commit|push)([^[:alnum:]_-]|$)'; then
    [[ "$branch" =~ $AGENT_BRANCHES ]] \
        || block "on '$branch' this is AI-assist work: leave the changes uncommitted and give a handover; the owner commits."
fi
exit 0
