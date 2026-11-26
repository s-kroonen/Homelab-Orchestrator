# Implementation Plan: [FEATURE]

**Branch**: `[###-feature-name]` | **Date**: [DATE] | **Spec**: [link]

**Input**: Feature specification from `/specs/[###-feature-name]/spec.md`

**Note**: This template is filled in by the `__SPECKIT_COMMAND_PLAN__` command; its definition describes the execution workflow.

## Summary

[Extract from feature spec: primary requirement + technical approach from research]

## Technical Context *(from the stack decision, not chosen here; dev-standard preset)*

<!--
  GATE: open docs/decisions/0002-choose-the-stack.md. If its Status is not "accepted", STOP:
  feature plans don't choose the stack (`scripts/check fast` fails while a plan exists and the
  decision isn't accepted). Run the stack research first (/stack-concerns, /stack-research, /stack-decide).
  Copy the values below from the accepted decision and from CONSTITUTION.md "Project Constraints".
  Something this feature needs that the decision doesn't cover (a new language, framework,
  storage or platform) is a NEW decision record, proposed here and accepted before implementation.
-->

**Stack decision**: `docs/decisions/0002-choose-the-stack.md` (status: accepted on [DATE])
**Language/Version**: [from the decision]
**Primary Dependencies**: [from the decision; new ones for this feature listed under "New dependencies" below]
**Storage**: [from the decision, or N/A]
**Testing**: [test command and safety/coverage commands from check.config]
**Target Platform**: [from the decision]
**Performance Goals / Constraints**: [from docs/product/quality-attributes.md, or NEEDS CLARIFICATION]

**New dependencies for this feature**: [none / name, version, why, licence; each one verified on the registry]
**Needs a new decision record?** [no / yes: which question, link to the proposed record]

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

[Gates determined based on constitution file]

## Guardrails & Security Check *(dev-standard preset)*

*GATE: pass before implementation; re-check before marking the plan done.*

- [ ] No change to guardrail files (`.guardrails`), or the change is listed here for the owner to make
- [ ] Every input from the Security & Data section has a validation step in this plan
- [ ] Every dangerous operation has a guard and a test (and, if the scanners can't see it, a canary entry)
- [ ] No new secret or personal data in code, tests, fixtures or docs
- [ ] `scripts/check all` is part of the verification steps

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (__SPECKIT_COMMAND_PLAN__ command output)
├── research.md          # Phase 0 output (__SPECKIT_COMMAND_PLAN__ command)
├── data-model.md        # Phase 1 output (__SPECKIT_COMMAND_PLAN__ command)
├── quickstart.md        # Phase 1 output (__SPECKIT_COMMAND_PLAN__ command)
├── contracts/           # Phase 1 output (__SPECKIT_COMMAND_PLAN__ command)
└── tasks.md             # Phase 2 output (__SPECKIT_COMMAND_TASKS__ command - NOT created by __SPECKIT_COMMAND_PLAN__)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths (e.g., apps/admin, packages/something). The delivered plan must
  not include Option labels.
-->

```text
# [REMOVE IF UNUSED] Option 1: Single project (DEFAULT)
src/
├── models/
├── services/
├── cli/
└── lib/

tests/
├── contract/
├── integration/
└── unit/

# [REMOVE IF UNUSED] Option 2: Web application (when "frontend" + "backend" detected)
backend/
├── src/
│   ├── models/
│   ├── services/
│   └── api/
└── tests/

frontend/
├── src/
│   ├── components/
│   ├── pages/
│   └── services/
└── tests/

# [REMOVE IF UNUSED] Option 3: Mobile + API (when "iOS/Android" detected)
api/
└── [same as backend above]

ios/ or android/
└── [platform-specific structure: feature modules, UI flows, platform tests]
```

**Structure Decision**: [Document the selected structure and reference the real
directories captured above]

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |

## Departures during build *(dev-standard preset)*

<!--
  Filled in during implementation: every place where the build left this plan, with the reason and
  who agreed. Empty means the plan was followed exactly.
-->

- [none yet]

