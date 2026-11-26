---
name: stack-layer
description: "Walk the stack-layer playbook (docs/process/stack-layer.md) after the stack decision is accepted and when the codebase grows. Finds what is still undecided, proposes the project.config lines, and says which gates are proven. Use after /stack-decide was accepted, or when `scripts/check stack-layer` warns."
---

# Stack layer

`project.config`, `check.config` and the CI files are guardrail files: the agent does not edit them. It finds what is
missing, proposes the exact lines, and a person applies them (or approves the PR with the label
`guardrail-change`). Read `docs/process/stack-layer.md` first; it is the checklist.

1. **Current state.** Read `docs/decisions/0002-choose-the-stack.md` (status `accepted`; otherwise
   stop), `.copier-answers.yml` (is `stack:` set?), `docs/product/quality-attributes.md`.
   Run `scripts/check stack-layer` and `scripts/check quality-canary`; paste both outputs.
2. **Step 1 of the playbook** (apply the profile): while `stack` is `none`, a person runs
   `copier update --trust --data stack=<name>`. Then the quality canary must pass.
3. **Each remaining step**, in the playbook's order: check whether the project needs it (a UI? an
   HTTP API? parsers of untrusted input? a Dockerfile?). For each:
   - needed: propose the command for this stack (the playbook has an example per stack), run it
     once in the working tree to see it work, and report the result;
   - not needed: propose `VAR="none"   # reason`.
   Every tool named is verified on its registry first (`scripts/verify-package`); only looked-up
   versions are pinned.
4. **Write the proposal** as a block of `project.config` lines in the handover or the PR (and CI
   changes as a diff). The guardrail files stay untouched.
5. **Prove the gates.** For each added gate, add the case it must fail on to the plan (a person adds
   it to `tests/`); the quality canary and security canary stay green.
6. **Hand over** (`/handover`): steps done, steps proposed with the lines, steps skipped with the
   reason, the `COVERAGE_TOTAL_MIN` proposal (the current total), and the research review (`<KEY>-6`).
