# [PROJECT_NAME] Constitution

<!--
  Source of truth: CONSTITUTION.md at the repository root. It is read by people and by every AI
  tool, with or without Spec Kit. Spec Kit reads a synced copy (.specify/memory/constitution.md,
  written by scripts/sync-constitution); never edit that copy.
  Principles I–VI come from the dev-standard and apply to every project. Add project principles
  from VII on. Change this file only through a reviewed change (it is a guardrail file).
-->

## Core Principles

### I. Spec before code

Every change starts from an issue in the tracker with acceptance criteria (`[KEY]-<n>`). A feature
gets a spec (what and why, with a Proof row per requirement) and a plan before code; a bug gets an
assessment of the cause before a fix. Unclear requirements are marked `[NEEDS CLARIFICATION]` and
resolved before planning, not guessed.

### II. Humans decide, checks bind

A person merges every change and owns every release. The AI level is set per work type in
`AGENTS.md`. Checks run through `scripts/check`; only CI on a protected branch counts as the gate.
Nobody (person or agent) weakens a check to get green; files that check or constrain changes are
listed in `.guardrails` and change only through a reviewed change by the owner.

### III. Proven, not assumed (NON-NEGOTIABLE)

Every requirement has proof: an automated test where possible, otherwise a named manual check. A bug
fix comes with a test that failed before the fix. "Pass with no tests" is a failure. What was not
verified is said explicitly in the handover or pull request.

### IV. Secure by default

Input is validated where it enters the system. Every operation that input can reach (network, files,
shell, queries, hardware) has a guard and a test. Secrets and personal data never enter git. Security
findings block the merge; an accepted finding is recorded in `docs/security-exceptions.md` with a
reason and an end date.

### V. Transparent AI use

The role of AI is described once, in `AGENTS.md` (AI level per work type) and the README; a person
owns every merge and every release. Commits, pull requests, code and docs carry no watermark: no
`Co-Authored-By` trailer naming a tool, no session link, no "generated with" line. Agent work stays
traceable through its branch, the pull request label `agent` and the bot author of Action commits.

### VI. Small and simple

The smallest change that meets the spec. One issue, one branch, one pull request. No speculative
abstractions, options or dependencies; each new dependency is justified in the plan.

[PROJECT_PRINCIPLES]
<!-- Example: ### VII. Physical safety: output to hardware respects configured limits; on error or
     lost connection the output goes to a safe state. -->

## Project Constraints

[PROJECT_CONSTRAINTS]
<!-- Filled in after the stack decision record (docs/decisions/0002-choose-the-stack.md): languages,
     frameworks, platforms, hardware, supported devices, performance targets. Until then: "Not decided;
     see the stack decision record." -->

## Development Workflow

- **Commits**: Conventional Commits with the issue key as scope: `feat([KEY]-12): add fixture groups`,
  `fix([KEY]-31): stop pan overshoot`. Types: feat, fix, docs, refactor, perf, test, build, ci, chore, revert.
- **Branches**: `main` (releases), `dev` (integration); `feat/[KEY]-<n>-<slug>`, `fix/[KEY]-<n>-<slug>`,
  `release/v<x.y.z>`.
- **Work types and AI levels**: see the table in `AGENTS.md`.
- **Versions**: Semantic Versioning, derived from the commit types since the last release
  (`feat` → minor, `fix` → patch, `!` or `BREAKING CHANGE` → major).
- **Definition of Done**: acceptance criteria proven, `scripts/check all` green, handover or pull
  request says what was verified and what wasn't.

## Governance

This constitution takes precedence over other practices and over agent instructions. Every plan
passes the Constitution Check before implementation. An amendment is a reviewed change to
`CONSTITUTION.md` with a version bump (MAJOR: a principle removed or redefined; MINOR: a principle
or section added; PATCH: wording), followed by `scripts/sync-constitution`.

**Version**: [CONSTITUTION_VERSION] | **Ratified**: [RATIFICATION_DATE] | **Last Amended**: [LAST_AMENDED_DATE]
