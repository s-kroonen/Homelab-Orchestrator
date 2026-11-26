## Issue

<!-- One issue per PR. Example: APP-56 -->
Closes:

## What and why

<!-- 2–5 sentences, present tense, no first or second person (docs/process/writing.md). What changes for a user, admin or developer, and why. -->

## Acceptance criteria

<!-- Copy them from the issue; tick what is met and say how it was verified. -->
- [ ] <criterion> — verified by: <test / manual step>

## How it was verified

- [ ] `scripts/check all` passes locally
- [ ] Manual check: <what was tried, on which device or environment>
- Not verified / known gaps: <say so, or "none">

## PRs labelled `agent`

<!-- The label marks agent work (set by the agent workflow or by the agent that opened the PR).
     The reviewer ticks these; CI covers what it can (test-integrity, new-deps, pr-size, guardrails). -->
- [ ] Every requirement has proof (spec Proof table or handover), and the reviewer checked one personally
- [ ] Tests not weakened; a bug fix has a test that fails without the fix
- [ ] Scope matches the plan or issue; nothing unrelated (follow-ups listed instead)
- [ ] New dependencies: named in the plan or ADR, real, maintained, licence fits
- [ ] Security & Data section of the spec still holds (input, secrets, personal data, safety)
- [ ] Handover read: what was not verified is acceptable or has an issue

## Risk

- Touches guardrail files (.guardrails)? <no / yes: which, why>
- Database or API contract change? <no / yes: migration, linked issue in the other repo>
- Rollback: <revert / redeploy previous image / migration down>
