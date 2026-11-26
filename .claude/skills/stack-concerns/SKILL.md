---
name: stack-concerns
description: "First step of choosing the stack. Turn the product vision and the idea assessment into the architecturally significant requirements (timing, safety, platforms, hardware, offline, packaging, security) and the list of technical concerns the stack must answer. Use before /stack-research, after the assessment verdict is go."
---

# Stack concerns

Input: `docs/product/vision.md`, the assessment in `.specify/assessments/<slug>/` (if Spec Kit is used),
`docs/research/previous-version.md` (for a remake), `CONSTITUTION.md`.
Output: `docs/product/quality-attributes.md` (the template is there): the quality attributes, the
**Architecturally significant requirements** (ASRs) section, and a concern list.

1. **Quality attributes.** For each row in the template: a measurable target (for example "an output
   frame every ≤ 23 ms, jitter ≤ 2 ms", "starts offline", "runs on Windows 10+ without install
   rights"), the source (vision, assessment, constitution, previous version), and
   `[NEEDS CLARIFICATION]` where unknown. A number without a source is not invented: ask, or mark it.
2. **Concerns.** List the technical questions the stack must answer, one per row: what is decided
   (for example "device output over USB serial", "3D room UI", "desktop packaging"), why it matters
   (link the quality attribute), how reversible a wrong choice is (one-way or two-way door), and
   whether only code can answer it (a spike).
3. **ASRs.** Pick the quality attributes that are expensive to change later (timing, safety,
   platforms, hardware, offline, packaging, security) and write each as a testable statement with
   its source in the ASR table. The playbook revisits them after the first working build.
4. **Previous version.** For a remake, every concern lists what v1 did and what the project learned.
5. Add a `TODO.md` item per concern that needs research (`<KEY>-?`: the owner assigns numbers) and per
   spike, with a time-box.
6. Hand over (`/handover`). The choice of technology belongs to `/stack-research` and `/stack-decide`.
