---
name: stack-decide
description: "Write the stack decision record (docs/decisions/0002-choose-the-stack.md) from the quality attributes, the per-concern research and spike results. Use when every concern has research (and spikes where needed). The owner accepts it; the agent never sets it to accepted."
---

# Stack decision

1. Check inputs: every concern in `docs/product/quality-attributes.md` has a research note; every
   one-way-door concern marked "needs spike" has a spike result. Missing input: stop and list it.
2. Re-verify every note past its `Re-check by` date (or older than `RESEARCH_MAX_AGE_DAYS`): versions,
   maintenance and licences with `scripts/verify-package`; update its Date, Verified and Re-check by.
   `scripts/check research` fails a proposed decision that links a stale note.
3. Fill `docs/decisions/0002-choose-the-stack.md`: context, drivers (quality attributes by ID),
   options per concern with links to the research notes, the decision per concern, consequences,
   the **Reversibility** section (one-way or two-way door and how to undo it), and the **Evidence**
   section listing every research note, spike and assessment used (links to files that exist).
4. Leave **Status: proposed**. Only the owner changes it to accepted.
5. Hand over the follow-ups that happen after acceptance:
   - `CONSTITUTION.md` → Project Constraints (via `/speckit-constitution`), then `scripts/sync-constitution`;
   - `AGENTS.md` sections 7–8 (owner: guardrail file);
   - stack layer: `copier update --trust --data stack=<python|node|rust|dotnet|flutter>`, then
     `scripts/check quality-canary` and `scripts/check canary` pass before the first feature;
   - drop the spike branches.
