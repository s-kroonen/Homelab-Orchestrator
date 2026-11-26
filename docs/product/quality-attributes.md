# Quality attributes and stack concerns

The requirements that decide the stack, each measurable. Filled by `/stack-concerns`; the stack
decision cites them by ID.

## Quality attributes

| ID | Attribute | Target (measurable) | Source | Status |
|---|---|---|---|---|
| QA-1 | Timing / real-time | <e.g. output frame ≤ 23 ms, jitter ≤ 2 ms> | | NEEDS CLARIFICATION |
| QA-2 | Safety | <limits, safe state, reaction time> | constitution | |
| QA-3 | Platforms | <OS, versions, phone/tablet/browser> | | |
| QA-4 | Hardware interfaces | <devices, protocols, drivers> | | |
| QA-5 | Offline / network | <works without internet? LAN only?> | | |
| QA-6 | Packaging / install | <single exe, no admin rights, auto-update> | | |
| QA-7 | Security | <control surface, see docs/security/> | constitution | |
| QA-8 | Performance / scale | <fixtures, universes, frame rate of the UI> | | |
| QA-9 | Maintainability | <who maintains it, skills, AI-agent friendliness> | | |

## Architecturally significant requirements (ASRs)

The quality attributes (or parts of them) that would be expensive to change later: they decide the
architecture and the stack. Filled by `/stack-concerns`, checked against what was measured after the
first working build (`docs/process/stack-layer.md`, step 14), and cited by decision records.

| ID | Requirement (a testable statement) | Quality attribute | Source | Measured (date, how) | Gate |
|---|---|---|---|---|---|
| ASR-1 | <e.g. a frame is output every ≤ 23 ms, jitter ≤ 2 ms, on the target hardware> | QA-1 | | not yet | `PERF_CMD` |

## Concerns

| ID | Concern | Drivers | Reversibility | Needs a spike? | Research note | Spike |
|---|---|---|---|---|---|---|
| C-1 | <e.g. output to the device over USB serial> | QA-1, QA-4 | one-way / two-way | yes / no | `docs/research/stack-…md` | `spike/…` |
