# Security canary

Planted bugs that the security scanners **must** keep finding. `scripts/check canary` scans a copy of
this folder and fails when an id in `expected.txt` goes undetected, so a tool upgrade, a rule
change or a too-broad exclusion cannot silently blind the pipeline.

- This code is never built or run. It is excluded from the normal scans (`.semgrepignore`,
  `SKIP_DIRS`), from CodeQL (`.github/codeql/codeql-config.yml`) and belongs outside lint.
- Secrets are not stored here: the canary stage generates them at run time, so push protection and
  secret scanning stay quiet.
- Adding a scanner or a custom rule? Add a planted example here and its id to `expected.txt`.
