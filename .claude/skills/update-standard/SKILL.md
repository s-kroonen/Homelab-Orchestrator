---
name: update-standard
description: "Bring a new version of dev-standard into this project: run scripts/update-standard on its own branch, resolve conflicts only inside project sections, never edit standard files, and write the handover with the report. Use when scripts/check standard-version reports a newer version or the user asks to update the standard."
---

# Update from dev-standard

Read `docs/process/updating.md` first. Files are standard (replaced by the update), project
(untouched) or shared (merged, project section below the marker).

1. **Issue.** The update needs an issue `<KEY>-<n>` (AGENTS.md rule 1). Without one: stop and ask.
2. **Look.** Run `scripts/check standard-version` and read the upgrade notes. When an upgrade note asks
   for a decision (a new required check, a renamed file the project uses, a new label), put it in
   the handover as a question for a person; do not guess.
3. **Run.** `scripts/update-standard <KEY-n>` (add `--to vX.Y.Z` for a specific version). It creates
   `chore/<KEY-n>-update-dev-standard` from `dev`, runs the update and prints the report. A
   precondition failure (dirty tree, `main`, relative `_src_path`) is fixed as the message says;
   the update rewrites guardrail files, so the Claude Code sandbox and `protect-paths.sh` can refuse
   it: that is the rule working. Stop, ask a person to run the script in a terminal, and continue
   from its report. A relative `_src_path` is a guardrail file change (`.copier-answers.yml`) that
   a person makes.
4. **Conflicts.** For every file in the report with conflict markers:
   - shared file: keep the standard's lines above the marker, keep the project's lines below it;
   - standard file: take the standard's version (remove the markers, keep the new text) and note in the handover which project edit was lost;
   - never edit a standard file by hand and never weaken a check to get green.
5. **Check.** `scripts/check fast`, then `scripts/check all` where the tools are installed. Fix
   what fails in project files only; a failing standard check is reported, not changed.
6. **Commit and PR** (agentic level only; otherwise leave the working tree and give the handover):
   commit `chore(<KEY-n>): update dev-standard to vX.Y.Z`, push the branch, open the PR. List the
   changed guardrail files in the PR text: a person adds the label `guardrail-change`.
7. **Hand over** (`/handover`): the report of the script, the upgrade notes that need a person's
   action (`scripts/repo-settings apply`, new labels, new tools), conflicts and how each was resolved.

Never: merge, tag, edit `.copier-answers.yml` by hand, change `check.config`, or run `copier update`
with another Copier version than `COPIER_VERSION`.
