---
name: handover
description: "Write the handover at the end of a piece of work (feature help, bug fix, release, research): what changed, how each acceptance criterion was verified, what failed or wasn't verified, follow-ups. Use when finishing a task, before a person commits or reviews."
---

# Handover

Give the handover in the session (interactive) or as the PR description (agentic). In a headless
run without a PR, write it to `docs/handover/<KEY>-<n>.md` (git-ignored). Use the structure of
`docs/handover-template.md`. Rules:

- Factual and short. Name files by repository path.
- Every acceptance criterion: met or not met, and **how it was verified** (test name, command,
  manual step). Unverified is said plainly.
- `scripts/check` results: paste the summary line of `scripts/check all` (or of the stages run).
- Follow-ups: problems found outside the scope, each as a proposed tracker item (`TODO.md` row).
- Guardrail changes needed: anything in `.guardrails` that should change, and why. The file stays
  unchanged.
- At the AI-assist level, end with a proposed commit message (Conventional Commit with the issue key)
  for the person to use or rewrite.
- No watermark lines (`Co-Authored-By`, session links, "generated with"): the role of AI is
  described once, in `AGENTS.md` and the README.
