# 0002: Choose the stack

- **Status**: proposed
- **Date**: 2026-11-26
- **Deciders**: s-kroonen
- **Issue**: OR-4

## Context and problem

Homelab Orchestrator has no code yet. Before building, choose languages, frameworks, runtime, data
storage and deployment, based on the product vision (`docs/product/vision.md`) and the research
(`docs/research/`, `.specify/assessments/`).

## Decision drivers

- Principles in `CONSTITUTION.md` (tests, security, small and simple)
- <requirements from the vision: platforms, hardware, users, offline, performance>
- <what the team knows; what agents handle well>
- Tooling: linters, test runner, dependency scanning and SAST support for the language

## Options considered

<filled in during research>

## Decision

<pending>

## Reversibility

- **Door**: <one-way | two-way>
- **How to undo it**: <cost and steps to change the stack later; one-way: every concern needs
  research and, where only code can answer, a spike>

## Evidence

Links to the research notes, spike results and assessments this rests on (files that exist).
`scripts/check research` needs at least one before this record is accepted, and fails while a
linked note is past its re-check date.

- <one link per research note: `../research/stack-<concern>.md`>

## Consequences

When accepted: update `CONSTITUTION.md` (Project Constraints) and `AGENTS.md` (sections 7–8), and
add the stack layer (`copier update --data stack=<name>`, or by hand: commands in `project.config`,
CI setup, SAST rules, canary).
