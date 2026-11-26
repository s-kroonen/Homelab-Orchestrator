# Workflow — Homelab Orchestrator

How work flows from idea to release. Rules: `CONSTITUTION.md` (why) and `AGENTS.md` (how).

## Day 0 → first code

| Step | Output | Spec Kit |
|---|---|---|
| 1. Vision | `docs/product/vision.md` | by hand, or with the agent |
| 2. Assess the idea and research options | `.specify/assessments/<slug>/` | `/speckit-assess-intake` … `-decide` |
| 3. Project principles beyond I–VI | `CONSTITUTION.md` (VII+) | `/speckit-constitution` |
| 4. Quality attributes and concerns | `docs/product/quality-attributes.md` | `/stack-concerns` |
| 5. Research per concern; spikes where only code can answer | `docs/research/stack-*.md`, `spike/*` branches | `/stack-research` |
| 6. Stack decision (owner accepts) | `docs/decisions/0002-choose-the-stack.md` → accepted | `/stack-decide` |
| 7. Stack layer | the checklist [`stack-layer.md`](stack-layer.md): profile, boundaries, complexity, performance, UI, contracts, security tools, SAST rules, fuzzing, DAST, Docker, coverage; every variable in `project.config` decided | `copier update --trust --data stack=<name>`, then `/stack-layer` |
| 8. First working build | review the research: ASRs, retrospective (remake), added concerns, stale notes | step 14 of the playbook |

Research notes are dated, verified against the registries and re-checked
(`docs/research/README.md`); `scripts/check research` enforces it.

## Feature (agentic)

1. Item in the tracker with acceptance criteria; branch `feat/OR-<n>-<slug>`.
2. `/speckit-specify` → `/speckit-clarify` → `/speckit-plan` → `/speckit-tasks` → `/speckit-analyze`. Review the spec (Proof, Security & Data) and the plan (Constitution and Guardrails gates) before code.
3. `/speckit-implement`, then `/speckit-converge` until converged. The Stop hook runs the checks.
4. The agent commits, pushes and opens the PR; a person reviews and merges.

## Bug (agentic)

1. Item with the symptom and how to reproduce it.
2. The agent: `/speckit-bug-assess` (cause) → `/speckit-bug-fix` → `/speckit-bug-test` (verdict `verified`).
3. Branch `fix/OR-<n>-<slug>`, commit `fix(OR-<n>): …`, PR with the handover. A person merges.
4. **Escalation**: design change, public contract, security, data or hardware safety, a new dependency or > ~200 lines → the agent stops with a handover; the item's level becomes `ai-assist`.

## Parallel work and hand-over

- **One issue per worktree:** `git worktree add ../<repo>-OR-<n> <branch>`. Each worktree
  has its own branch, Spec Kit state (`.specify/feature.json`) and Stop-hook state, so two agents
  (or an agent and a person) don't overwrite each other. Remove it after the merge (`git worktree remove`).
- **Person ↔ agent:** a person may take over any branch at any time and records it by setting the
  item's Level to `ai-assist`. An agent continues only a branch it created or one it is asked to
  continue. After a switch, the one who continues reads the last handover first.

## Review of agent work

`/quality-review` asks the read-only `quality-reviewer` subagent for an independent check against the
spec's Proof table, tests, scope, constitution, dependencies and security; paste its output into the PR.
CI adds `proof` (the spec's Issue matches the branch, every FR-/SC- has proof) and a scope warning.

## Release (agentic)

1. `/release` (or by hand): `scripts/next-version` computes the version from the commits since the
   last tag; `scripts/next-version --notes` writes the changelog section.
2. The agent creates `release/v<x.y.z>`, updates `CHANGELOG.md` (and the version file, once the stack has one), commits `chore(release): v<x.y.z>` and opens the PR into `main`.
3. A person reviews, merges into `main`, tags `v<x.y.z>` and pushes the tag; then merges `main` back into `dev`.
4. The tag runs the release workflow: checks, SBOM, `SHA256SUMS` signed keyless with cosign, provenance
   attestation, GitHub release with the changelog section, back-merge PR (GitLab: tag pipeline with SBOM, release and MR).

Verify a release (GitHub): `cosign verify-blob --bundle SHA256SUMS.sigstore.json --certificate-identity-regexp '^https://github.com/<owner>/<repo>/' --certificate-oidc-issuer https://token.actions.githubusercontent.com SHA256SUMS`,
then `sha256sum -c SHA256SUMS`.

## Spikes

A spike answers one technical question with throwaway code (for example "can this runtime drive the
device at 44 Hz on Windows?").

- Branch `spike/OR-<n>-<slug>`, code under `spikes/<slug>/`, a time-box in `TODO.md`.
- The project's gates don't apply to `spikes/`; `scripts/check fast` fails if `spikes/` reaches any
  other branch, so spike code never lands on `dev` or `main`.
- The result goes into the research note (what was tried, measurements, the answer). Then the branch
  is deleted. Production code is written fresh, under the gates, after the stack decision.
