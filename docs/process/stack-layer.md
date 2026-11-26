# Stack layer: what to do after the first code

Run this after the stack decision is **accepted** (`docs/decisions/0002-choose-the-stack.md`) and
again when the codebase grows (another part, another kind of input, a UI). Order matters: later
steps need the gates of the earlier ones. The agent skill `/stack-layer` walks the list.

How it is enforced: every variable below lives in `project.config` (the standard's `check.config` is replaced by updates). Each is a **command** or
`"none"` with a reason on the same line (`E2E_CMD="none"   # no UI`). While source files exist and a
variable is empty, `scripts/check stack-layer` warns locally and **fails in CI**. Day 0 (no code)
passes. `project.config` is a guardrail file: a person changes it (agents propose the lines).

The commands below are **examples, not pinned**: look up the current version of every tool
(`scripts/verify-package <ecosystem> <name>`), pin it in the lockfile or the command, and run the
command once before relying on it. Paths are placeholders for the project's own.

| # | Step | Variable | Stage |
|---|---|---|---|
| 1 | Apply the profile | `stack` (Copier) | `quality-canary` |
| 2 | Architecture boundaries | `ARCH_CMD` | `architecture` |
| 3 | Complexity and duplication | `COMPLEXITY_CMD` | `complexity` |
| 4 | Performance budget | `PERF_CMD` | `perf` |
| 5 | UI gates | `E2E_CMD` | `e2e` |
| 6 | Contracts between parts | `CONTRACT_CMD` | `contract` |
| 7 | Language-native security tools | `SECURITY_NATIVE_CMD` | `security-native` |
| 8 | Stack SAST rules | (rules + canary) | `sast`, `canary` |
| 9 | Fuzzing | `FUZZ_CMD` | `fuzz` |
| 10 | Running-app scan | `DAST_CMD` | `dast` |
| 11 | Docker | Dependabot, CI | `image` |
| 12 | Coverage ratchet | `COVERAGE_TOTAL_MIN` | `coverage` |
| 13 | .NET only: verify the profile | | `quality-canary` |
| 14 | Review the research | | `research` |

## 1. Apply the profile

- **Why:** until the stack is set, source files fail `code-needs-gates`; the profile gives format,
  lint, typecheck, tests, test count, coverage and the CI toolchain, proven by the quality canary.
- **When:** right after ADR 0002 is accepted.
- **How:** `copier update --trust --data stack=<python|node|rust|dotnet|flutter>`; review the diff.
  Then make the project's own config skip the canary folders (they hold planted problems):
  Python: `extend-exclude` in `pyproject.toml` / `ruff.toml` (the profile passes it on the command
  line; the editor config mirrors it); Node: `tsconfig.json` `include` on the source folders only (not
  `.`), `eslint.config.mjs` ignores, `.prettierignore`; Rust: nothing (the canary has its own workspace).
- **Done when:** `scripts/check quality-canary` and `scripts/check canary` pass, `scripts/check fast`
  and `test` pass on a first small module, and the profile's CI toolchain installs in CI.

## 2. Architecture boundaries

- **Why:** documented layers are real only when a tool fails as soon as someone crosses them.
- **When:** as soon as there are two or more parts (modules, packages, crates). Write the layers
  in an ADR first (what may import what).
- **How:**

| Stack | Tool and example |
|---|---|
| Python | import-linter: contracts in `pyproject.toml`, `ARCH_CMD="uv run lint-imports"` |
| Node | dependency-cruiser: `.dependency-cruiser.cjs`, `ARCH_CMD="npx depcruise src --config"` |
| Rust | split into crates so the compiler enforces it; `ARCH_CMD="cargo tree --workspace --edges normal"` plus a test that parses it, or cargo-modules |
| .NET | NetArchTest in the test project: `ARCH_CMD="dotnet test --no-restore --filter Category=Architecture"` |
| Flutter | an import lint (custom_lint or `dart run import_lint`): `ARCH_CMD="dart run custom_lint"` |

- **Variable:** `ARCH_CMD`. **Done when:** a planted wrong import makes the command fail, and the
  ADR names the layers and the config file that enforces them.

## 3. Complexity and duplication

- **Why:** agents write long functions and copy code; a ceiling keeps the code reviewable.
- **When:** with the first real code; set the ceiling a little above today's worst function, then
  lower it.
- **How:**

| Stack | Tool and example |
|---|---|
| Python | ruff rule `C90`: `COMPLEXITY_CMD="uv run ruff check --select C901 --config 'lint.mccabe.max-complexity=10' src"` |
| Node | eslint `complexity` rule (in `eslint.config.mjs`) and jscpd for duplication: `COMPLEXITY_CMD="npx eslint --max-warnings=0 src && npx jscpd src"` |
| Rust | clippy `cognitive_complexity`: `COMPLEXITY_CMD="cargo clippy --all-targets -- -D clippy::cognitive_complexity"` |
| .NET | analyzer CA1502 (cyclomatic complexity) as an error in `.editorconfig`; `COMPLEXITY_CMD="dotnet build --no-restore -warnaserror"` |
| Flutter | `dart_code_metrics` successor `dcm`, or analyzer lints: `COMPLEXITY_CMD="flutter analyze --fatal-infos"` |

- **Variable:** `COMPLEXITY_CMD`. **Done when:** a planted 40-branch function fails it.

## 4. Performance budget

- **Why:** a timing requirement that nobody measures is a wish. Budgets come from the quality
  attributes (`docs/product/quality-attributes.md`, QA-*): quote the QA id next to the number.
- **When:** when a QA has a number (frame time, start-up, throughput) and there is code to measure.
- **How:**

| Stack | Tool and example |
|---|---|
| Python | pytest-benchmark: `PERF_CMD="uv run pytest -q benchmarks --benchmark-only --benchmark-max-time=1"` with an assertion on the stats |
| Node | `vitest bench`: `PERF_CMD="npx vitest bench --run"`, budgets asserted in the bench file |
| Rust | criterion: `PERF_CMD="cargo bench --locked"`; compare with a saved baseline |
| .NET | BenchmarkDotNet: `PERF_CMD="dotnet run -c Release --project bench"` |
| Flutter | `flutter test benchmark`; frame timing with integration tests |

- **Variable:** `PERF_CMD`. **Done when:** the command fails when a budget is exceeded (the budget
  is an assertion, not a printed number). Without a measurable QA: `none  # no timing requirement`.

## 5. UI gates

- **Why:** a UI breaks in ways unit tests miss: it does not start, a flow is gone, it is
  unusable with a keyboard, the bundle doubled.
- **When:** the project has a UI (web, desktop webview, mobile web).
- **How:** Playwright with `@axe-core/playwright` for accessibility and a smoke flow:
  `E2E_CMD="npx playwright test"`; Python projects with a web UI use `uv run pytest tests/e2e`
  with `pytest-playwright`. Bundle budget: `size-limit` or a `du`/`stat` assertion in CI:
  `E2E_CMD="npx playwright test && npx size-limit"`. Flutter: `flutter test integration_test`.
- **Variable:** `E2E_CMD`. **Done when:** a missing label or a 2x bundle fails it. No UI: `none  # no UI`.

## 6. Contracts between parts

- **Why:** the backend and its clients (UI, other services) drift apart silently.
- **When:** two parts talk over HTTP, WebSocket or files that someone else reads.
- **How:** keep the schema in the repository (OpenAPI, AsyncAPI for WebSocket, JSON Schema for
  files). Check three things: the schema is valid (`npx @redocly/cli lint openapi.yaml`), it did
  not change incompatibly (`oasdiff breaking origin/dev:openapi.yaml openapi.yaml`), and the server
  honours it (schemathesis against a test instance, or consumer-driven contract tests).
  `CONTRACT_CMD="npx @redocly/cli lint openapi.yaml && schemathesis run openapi.yaml --url $URL"`.
- **Variable:** `CONTRACT_CMD`. **Done when:** a changed response field fails it.
  A single deployable with no interface of its own: `none  # one process, no external interface`.

## 7. Language-native security tools

- **Why:** trivy and opengrep are general; each ecosystem has sharper tools for its own packages.
- **When:** with the first dependencies.
- **How:**

| Stack | Tool and example |
|---|---|
| Python | `SECURITY_NATIVE_CMD="uv run pip-audit --strict && uv run bandit -q -r src"` |
| Node | `SECURITY_NATIVE_CMD="npm audit --omit=dev --audit-level=high && npm audit signatures"`; install with `npm ci --ignore-scripts` (the profile does) |
| Rust | cargo-deny with a `deny.toml` (advisories, bans, sources): `SECURITY_NATIVE_CMD="cargo deny check"`; forbid `unsafe` in the crates that don't need it (`#![forbid(unsafe_code)]`) |
| .NET | NuGetAudit as errors in `Directory.Build.props`; `SECURITY_NATIVE_CMD="dotnet list package --vulnerable --include-transitive"` |
| Flutter | `SECURITY_NATIVE_CMD="dart pub outdated --no-dev-dependencies"` plus osv-scanner on `pubspec.lock` (`deps-report`) |

- **Variable:** `SECURITY_NATIVE_CMD`. **Done when:** a planted vulnerable dependency (in a throwaway
  branch) makes it fail.

## 8. Stack SAST rules

- **Why:** the open opengrep rules miss framework-specific sinks (tested: TypeScript 5 of 8,
  Rust 1 of 6, C# 2 of 7). Dart has no rules at all.
- **When:** the profile has custom rules for the frameworks it knows (`.opengrep/rules/custom/`).
  For a framework or language without them (for example Dart, or a framework the project chose
  itself): write the rules and the planted cases.
- **How:** one rule per vulnerability class in `.opengrep/rules/custom/<framework>-security.yaml`
  (taint mode: input source to dangerous sink); one planted file per class in
  `tests/security-canary/<stack>/` with a tagged line (`V:CMDI`); the rule id in
  `tests/security-canary/expected.txt`. Same way as the existing `stacks/*/rules` of dev-standard.
  For Dart: also a review checklist in `docs/` until rules exist, and `flutter analyze` with strict lints.
- **Done when:** `scripts/check canary` lists the rule ids and fails when a rule is removed.
  (Rules and canary files are guardrail files: a person adds them.)

## 9. Fuzzing

- **Why:** parsers of untrusted input (file formats, network messages, imports) fail on inputs
  nobody wrote a test for.
- **When:** the code parses something it did not produce itself.
- **How:** a short run in every CI run, a long run in the weekly CI job (`FUZZ_SECONDS=600`);
  write the command with `${FUZZ_SECONDS:-30}`.

| Stack | Tool and example |
|---|---|
| Python | hypothesis (property tests, in the normal tests) or atheris: `FUZZ_CMD="uv run python fuzz/parse_fuzz.py -max_total_time=${FUZZ_SECONDS:-30}"` |
| Node | fast-check in vitest, or Jazzer.js: `FUZZ_CMD="npx vitest run fuzz"` |
| Rust | cargo-fuzz: `FUZZ_CMD="cargo fuzz run parse -- -max_total_time=${FUZZ_SECONDS:-30}"` |
| .NET | SharpFuzz with libFuzzer or AFL |
| Flutter | property tests with `glados`; no mature coverage-guided fuzzer |

- **Variable:** `FUZZ_CMD`. **Done when:** the target runs and a planted crash is found. No such
  input: `none  # no untrusted parsers`.

## 10. Running-app scan

- **Why:** some problems only show in the running app (headers, cookies, exposed paths).
- **When:** the app serves HTTP and can be started in CI with test data.
- **How:** a script in the repo that starts the app, waits for it, runs ZAP baseline against it
  (`zap-baseline.py -t http://localhost:<port>` from the official ZAP container, image pinned by
  digest looked up beforehand), and stops the app. `DAST_CMD="scripts/dast.sh"`. Report first; make the
  baseline fail on warnings once the first findings are triaged.
- **Variable:** `DAST_CMD`. **Done when:** CI runs it against a real instance. No HTTP: `none  # no HTTP service`.

## 11. Docker

- **Why:** an image has its own vulnerabilities and its own base-image updates.
- **When:** there is a `Dockerfile`.
- **How:** add a `docker` entry for the Dockerfile's folder in `.github/dependabot.yml` (with the
  14-day cool-down); the `image` CI job already builds it and runs `scripts/check image`. Pin base
  images by tag and digest.
- **Done when:** `scripts/check image <ref>` passes locally and in CI, and Dependabot proposes base-image updates.

## 12. Coverage ratchet

- **Why:** `COVERAGE_TOTAL_MIN=0` lets coverage rot while changed lines stay covered.
- **When:** after the first real tests, and each time coverage rises durably.
- **How:** run `scripts/check coverage`, read the total it prints, set `COVERAGE_TOTAL_MIN` to that
  number (rounded down). Never lower it.
- **Variable:** `COVERAGE_TOTAL_MIN`. **Done when:** it is above 0 and `scripts/check stack-layer` stops warning.

## 13. .NET only: verify the profile

- **Why:** the `dotnet` profile was written without a .NET SDK and is **unverified**.
- **How:** in an environment with the SDK: add `tests/quality-canary/dotnet/` with the same case set
  as the Python profile (format, lint, typecheck, test fails, zero tests, skipped test, clean
  control); confirm that `dotnet test` with zero tests fails the test-count check, that
  `TreatWarningsAsErrors` makes the lint gate fail, and that the coverage file path is right.
- **Done when:** `scripts/check quality-canary` passes with all cases, and the profile's comment in
  `check.config` (stack profile) has lost the UNVERIFIED note; the profile belongs to the standard, so a person reports the result to dev-standard.

## 14. Review the research (after the first working build)

- **Why:** research happens before code exists. The build shows which assumptions held.
- **How:** (1) update the architecturally significant requirements in
  `docs/product/quality-attributes.md` with what was measured; (2) for a remake, write
  `docs/research/previous-version-retrospective.md` from `retrospective-template.md` while the
  old and new versions can still be compared; (3) re-run `/stack-concerns` for concerns that came up
  during the build; (4) mark research notes that have stopped holding `Status: stale`
  (`scripts/check research` then keeps them out of decisions). Tracker item: `OR-6`.
- **Done when:** `scripts/check research` passes and the ASR table says what was measured.
