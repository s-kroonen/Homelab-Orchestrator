---
name: release
description: "Prepare a release: compute the next version from the Conventional Commits since the last tag, update CHANGELOG.md, and put it on a release branch for a person to review, merge and tag. Use when the user asks to release, cut a version, or prepare a changelog."
---

# Prepare a release

Spec Kit has no release process; dev-standard adds this one. A person always merges and tags.

1. **Check the state.** On `dev`, clean working tree, up to date with the remote if there is one.
   Run `scripts/check all`; stop and report if it fails.
2. **Version.** Run `scripts/next-version`.
   - Exit 3 means nothing is releasable since the last tag: report that and stop.
   - The version is computed, never picked by hand; a wrong-looking version means wrong commit
     types: report which commits and stop.
3. **Branch.** Create `release/v<version>` from `dev` (allowed only if releases are agentic in
   `AGENTS.md`; otherwise prepare the files and leave them uncommitted).
4. **Changelog.** Run `scripts/next-version --notes` and insert its output in `CHANGELOG.md` directly
   under `## [Unreleased]`. Keep `## [Unreleased]` empty above it. Older entries stay as they are.
   Entries describe the product change ("Fixture groups can be renamed"), not the work.
5. **Version file.** If the stack has one (`package.json`, `pyproject.toml`, `pubspec.yaml`, …), set
   the version there too. Day 0 has none.
6. **Commit** `chore(release): v<version>` and push the release branch (agentic only).
7. **Hand over** (`/handover`): version, why that bump (the commits that decided it), what is in it,
   and the steps for the person:
   - review and merge `release/v<version>` into `main`;
   - `git tag v<version>` on `main` and push the tag;
   - merge `main` back into `dev`.

Never: tag, merge, push to `main` or `dev`, or edit an older changelog entry.
