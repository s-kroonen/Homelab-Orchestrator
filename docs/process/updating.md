# Updating from dev-standard

A new version of dev-standard reaches this project with one command, without merge conflicts as
long as the project edits only its own parts. This page lists which parts those are.

## Three kinds of files

| Kind | Files | An update |
|---|---|---|
| **Standard files** | `scripts/`, `.claude/` (hooks, skills, agents, `settings.json`), the workflows (`.github/`, `.gitlab-ci.yml`), `check.config`, `.vale/styles/DevStandard/`, `.vale.ini`, the canaries under `tests/`, `tests/hooks/cases.tsv`, `.opengrep/rules/custom/`, `docs/process/`, the templates under `docs/` | replaces them; the project never edits them |
| **Project files** | `README.md`, `CHANGELOG.md`, `TODO.md`, `project.config`, `docs/product/`, `docs/decisions/0002-*`, `docs/security-exceptions.md`, `.trivyignore.yaml`, `osv-scanner.toml`, `.opengrep/rules/project/`, `.vale/styles/Project/` with its vocabulary, `tests/hooks/project-cases.tsv` | never touches them; a missing one is created |
| **Shared files** | `AGENTS.md`, `CLAUDE.md`, `CONSTITUTION.md`, `.guardrails`, `lefthook.yml`, `.gitignore`, `CODEOWNERS` | merges them: Copier keeps the project's lines and applies the standard's changes |

Project files are listed under `_skip_if_exists` in the standard's `copier.yml`. Each shared file
has one marked project section: `<!-- project rules: below this line -->` in the Markdown files,
`# project: below this line` in the others. The standard never edits what follows the marker (in
`CONSTITUTION.md`, the part between the marker and "Development Workflow"). A project edits only
below the marker.

## Project settings: `project.config`

`check.config` holds the standard's rules, pinned tools and the stack profile. `project.config` is
sourced after it by `scripts/check`, `scripts/setup`, `scripts/update-rules` and the hooks, so
its values win. It holds coverage thresholds, `VALE_EXCLUDE`, the stack-layer variables
(`ARCH_CMD` and the others) and any override of a command or rule. Both are guardrail files.

Project extension points that updates never touch: `.opengrep/rules/project/` (extra SAST rules),
`.vale/styles/Project/` and `.vale/styles/config/vocabularies/Project/accept.txt` (writing rules and
accepted terms), `tests/hooks/project-cases.tsv` (extra hook cases, run after the standard cases).

## The update

```bash
scripts/check standard-version       # is a newer version out? (weekly CI job; never fails)
scripts/update-standard APP-12       # branch chore/APP-12-update-dev-standard from dev
scripts/update-standard APP-12 --to v0.3.0
```

The script refuses a dirty working tree and `main`, reads `_src_path` and `_commit` from
`.copier-answers.yml`, runs the pinned Copier (`COPIER_VERSION` in `check.config`) with
`copier update --trust --defaults --skip-tasks --conflict inline --vcs-ref <tag>` and then runs the post steps:
`scripts/post-update` (the constitution copy of Spec Kit, vendored rule sets), the Spec Kit preset
when it changed, `scripts/setup` when a pinned tool changed. It never commits.

The report lists:

- the upgrade notes of every version in between (manual steps, renamed or removed files, new labels,
  new required checks, new tools);
- files with conflict markers;
- changed guardrail files (the PR needs the label `guardrail-change`);
- whether `scripts/repo-settings apply` is needed after the merge (labels or required checks changed);
- the result of `scripts/check fast`;
- the commit message `chore(<KEY-n>): update dev-standard to vX.Y.Z`.

A project generated before v0.3.0 makes its first jump with `copier update --trust --defaults
--skip-tasks --conflict inline --vcs-ref <tag>`, then `scripts/post-update`: the tasks of the old
release run in a temporary copy without git, and on bash 5.3 they stop the update.

A plain `copier update --trust --vcs-ref <tag>` from v0.3.0 on stays correct: a Copier migration runs
`scripts/post-update` after the merge. It does not run the Spec Kit preset, `scripts/setup` or the
report.

## Conflicts

A conflict (inline markers `<<<<<<<` and `>>>>>>>`) appears only where the project and the
standard changed the same lines.

- In a shared file: resolve inside the project section (below the marker) and keep the standard's
  lines above it.
- In a standard file: the project edited a file it must not edit. Take the standard's version and
  move the project's need into a project file (`project.config`, an extension point) or into a
  change request to dev-standard.
- In a project file: not possible; updates do not touch them.

The update rewrites guardrail files (`scripts/`, `.claude/`, the workflows), which the sandbox of
Claude Code and `protect-paths.sh` keep read-only for an agent. A person runs the script in a
terminal, or an agent runs it where those protections do not apply (a CI job); the agent then
resolves conflicts and writes the handover. The skill `/update-standard` does this for an agent: it runs the script, resolves conflicts only
inside project sections, never edits a standard file and writes the handover with the report.

## Source of the standard

`_src_path` in `.copier-answers.yml` must be `gh:<owner>/dev-standard` (or an absolute path or URL
of a git repository with release tags). A relative path stops `copier update` with "Updating is
only supported in git-tracked templates"; `scripts/update-standard` explains the fix.

## Projects generated before `check.config` became a standard file (before v0.3.0)

The update to v0.3.0 or later runs a Copier migration: the values of the project in `check.config`
(the sections `project` and `stack layer playbook` and any edited rule) move into `project.config`,
and `check.config` becomes the standard's file again. Edited pinned-tool versions are dropped; the
standard owns the pins. Review `project.config` and the diff of `check.config` in the PR.

## What Copier does (verified with 9.7.1)

- `_skip_if_exists` also applies on update: an existing file is kept as it is, a missing one is created.
- `_tasks` run twice on update: in the project directory after the new standard files are
  rendered and before the merge restores the project's edits, and in a temporary copy that is not a
  git repository. A task never sees merged shared files. `--skip-tasks` skips both. Steps that read
  merged files are migrations; they run in the project directory, also with `--skip-tasks`.
- `_migrations` run in the project directory. `before` runs ahead of the update, `after` behind the
  merge. An entry with a `version` runs when the update crosses that version; one without runs on
  every update.
