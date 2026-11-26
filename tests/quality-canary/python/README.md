# Quality canary (Python)

Each folder is a tiny standalone case with one planted problem. `scripts/check quality-canary`
runs the project's gates (check.config) inside each folder and expects:

| Case prefix | Gate that must fail |
|---|---|
| `clean-*` | none: every gate passes (control case) |
| `format-*` | FORMAT_CMD (ruff format) |
| `lint-*` | LINT_CMD (ruff) |
| `typecheck-*` | TYPECHECK_CMD (mypy) |
| `test-fails-*` | TEST_CMD (pytest) |
| `test-zero-*` | TEST_LIST_CMD lists 0 tests |
| `skip-marker-*` | SKIP_MARKERS matches |

The per-case `ruff.toml`, `mypy.ini` and `pytest.ini` keep the project's own config out. The project
gates exclude this folder. Don't fix these files.
