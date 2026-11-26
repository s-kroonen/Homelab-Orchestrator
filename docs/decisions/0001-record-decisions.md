# 0001: Record decisions as Markdown decision records

- **Status**: accepted
- **Date**: 2026-11-26
- **Deciders**: s-kroonen
- **Issue**: OR-1

## Context and problem

Decisions about stack, architecture and process get lost in chats and commit messages. Agents need
the reasons too, or they will undo them.

## Decision

Every significant decision gets a record in `docs/decisions/NNNN-title.md`, based on
[MADR](https://adr.github.io/madr) (`template.md`). A record is never rewritten after it is accepted;
a new record supersedes it.

## Consequences

- The reasons stay next to the code, readable by people and agents.
- Writing a record costs ~15 minutes; worth it for anything that is expensive to reverse.
