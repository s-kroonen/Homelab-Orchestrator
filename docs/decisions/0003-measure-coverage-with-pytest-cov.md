# 0003: Measure test coverage with pytest-cov

- **Status**: proposed
- **Date**: 2026-11-26
- **Deciders**: s-kroonen
- **Issue**: OR-99

## Context and problem

The `coverage` stage of `scripts/check` runs `pytest --cov --cov-report=xml:coverage.xml` (the
Python profile's `COVERAGE_CMD`) and fails when `coverage.xml` is missing. The project had no
coverage tool, so the stage could not run, and changed-line coverage (`COVERAGE_CHANGED_MIN=80`)
was not measured.

## Decision drivers

- Constitution III: every requirement has proof; untested changed lines are visible.
- The standard's Python profile expects the pytest `--cov` options.
- No new runtime dependency: a dev tool only.

## Options considered

### Option A: pytest-cov

- Good: the profile's command works unchanged; MIT licence; maintained by the pytest-dev
  organisation; `scripts/verify-package pypi pytest-cov 6.3.0` (2026-11-26): exists, MIT, version
  6.3.0 released 2025-09-06, latest 7.1.0 released 2026-03-21.
- Bad: one more dev dependency.

### Option B: coverage.py directly (`coverage run -m pytest`)

- Good: one package fewer (pytest-cov depends on it anyway).
- Bad: `COVERAGE_CMD` has to be overridden in `project.config`; the plugin's xdist and subprocess
  handling is lost.

## Decision

Chosen: **pytest-cov** (`~=6.0`, dev only), because the standard's command runs as it is and the
package is the common pytest integration of coverage.py.

## Reversibility

- **Door**: two-way
- **How to undo it**: remove `pytest-cov` from the `dev` extra and set `COVERAGE_CMD` to a
  `coverage run` command in `project.config`.

## Evidence

- [The standard's Python profile](../../check.config) (`COVERAGE_CMD`)
- Package check: `scripts/verify-package pypi pytest-cov 6.3.0`, output in OR-99

## Consequences

- Good: changed-line coverage is measured in CI.
- Bad, and how it is handled: the coverage run repeats the suite in CI (about 15 minutes); the
  Stop hook does not run it (`STOP_STAGES=fast`).
- Follow-up: set `COVERAGE_TOTAL_MIN` to the measured total (OR follow-up).
