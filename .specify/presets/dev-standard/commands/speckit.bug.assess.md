---
description: "dev-standard addendum to the bug assessment: escalation check"
---

## Escalation check (dev-standard addendum)

The assessment file MUST end with this section, filled in after the remediation is proposed. A bug
fix by an agent is small, local and boring; anything else goes to a person (AGENTS.md, section 2).

```markdown
## Escalation check

| Trigger | Yes / no | Evidence (file, line, number) |
|---|---|---|
| Changes the design or a public contract (API, file format, protocol) | | |
| Touches security, data handling or physical safety | | |
| Needs a new dependency | | |
| More than about 200 changed lines (estimate from "Files likely to change") | | |

**Escalate: yes / no.** One "yes" means yes.
```

If **Escalate** is yes: set `Verdict` to `needs a person`, do not run `__SPECKIT_COMMAND_BUG_FIX__`,
and tell the person which trigger fired. The item's Level in the tracker becomes `ai-assist`.
