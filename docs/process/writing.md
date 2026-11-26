# Writing rule

How text in this repository is written. The rule applies to docs, code comments, changelog entries,
`TODO.md` items, PR and MR text, and handovers. [Vale](https://vale.sh) enforces what a tool can
enforce (`scripts/check docs`, `scripts/check pr-text`); reviewers enforce the rest.

## The rule

<!-- vale off -->

1. **No first or second person.** No we, our, us, I, my, you, your. Name the thing: "The hook
   blocks force pushes", "dev-standard adds a gate".
2. **Present tense, describing what exists.** "The `deps` stage fails on known malicious packages."
3. **Steps use the imperative, without "you".** "Run `scripts/setup`."
4. **No history in docs or comments.** No now, new (as in "newly added"), no longer, previously,
   updated to, added by. History lives in git and `CHANGELOG.md`.
5. **Comments explain why, not what.** No commented-out code. A TODO in code carries an issue key:
   `TODO(KEY-12): handle a closed port`.
6. **No filler or marketing.** No simply, just, easily, robust, seamless, powerful, comprehensive,
   leverage, "note that", "it's worth noting". No emojis, no exclamation marks.
7. **No hedging.** No "should work", "hopefully", "probably". State what is verified and what is not.
8. **Changelog entries describe the product change.** "Fixture groups can be renamed", not "Renamed
   fixture groups".
9. **TODO item titles state the problem or the outcome.** "Pan overshoots at the end of a cue".
10. **No AI watermarks in any text.** No `Co-Authored-By` trailer naming an AI tool, no session
    link, no "generated with" line, no robot emoji. The role of AI is described once, in
    `AGENTS.md` and the README.
11. **Commit subjects are imperative Conventional Commits** (`fix(KEY-31): stop pan overshoot`).
    Vale does not read commit subjects; `scripts/check commit-msg` checks the format and the
    watermark ban.
12. **Text in another language** (domain data, quoted source material) is out of scope; list its
    paths in `VALE_EXCLUDE`.

<!-- vale on -->

## What the tools check

| Rule | Vale rule | Level |
|---|---|---|
| 1 | `DevStandard.Pronouns` | error |
| 4 | `DevStandard.History` | error |
| 5 | `DevStandard.TodoKey` (source comments) | error |
| 6 | `DevStandard.Filler`, `DevStandard.Exclamation`, `DevStandard.Emoji` | error |
| 7 | `DevStandard.Hedging` | error |
| 10 | `DevStandard.Watermarks` (also commit messages, PR titles, PR bodies through `WATERMARK_PATTERN`) | error |
| style | `DevStandard.SentenceLength` (over 30 words), `DevStandard.Passive` (passive with an actor), `DevStandard.HeadingCase`, `DevStandard.Substitutions`, `Vale.Spelling`, `Vale.Repetition` | warning |

Errors fail the check; warnings print. Rules 2, 3, 5 (comments explain why), 8 and 9 need a
reader. The bare word "new" stays allowed in phrases such as "new dependency" and "new branch";
`DevStandard.History` flags only its history use.

<!-- vale off -->

The flagged forms of "new" are "is new", "are new", "new in" and "new since".

<!-- vale on -->

## Where Vale runs

- `scripts/check docs` reads every Markdown file and the comments of every source file of a
  language below. In CI, a pull request run passes the base branch (`scripts/check docs origin/dev`)
  and reads the changed files only; pushes and the weekly run read everything.
- `scripts/check pr-text` reads the PR or MR body from the environment variable `PR_BODY`.
- The PostToolUse hook `.claude/hooks/lint-file.sh` runs Vale (and `LINT_FILE_CMD`) on each file
  an agent edits and reports problems to the agent; the edit itself stands.
- Spec Kit output (`.specify/`, `specs/`, the `speckit-*` skills), canary folders, `spikes/`,
  `docs/handover/` and vendored rules are skipped (`VALE_SKIP` in `scripts/check`).

## Languages whose comments Vale reads

Verified with Vale 3.26.0 by planting a first-person comment in each language:

| Read | Comment forms |
|---|---|
| Python | `#` comments, docstrings of modules, classes and functions |
| JavaScript, TypeScript (`.js .jsx .mjs .cjs .ts .tsx`) | `//`, `/* */`, `/** */` |
| Rust | `//`, `///`, `//!` (block comments are not read) |
| Go, C#, Java, Kotlin, Swift, C, C++, PHP, Scala | line and block comments |
| Ruby | `#` comments |

Not read, so reviewers cover them: Dart, shell, Lua, SQL, YAML, TOML, F#, Haskell. Strings are
never read.

## Configuration

- `.vale.ini` picks the rules per file type; the rules are in `.vale/styles/DevStandard/`.
  Both are guardrail files.
- The spelling check reads the project vocabulary in `.vale/styles/config/vocabularies/Project/accept.txt`
  (one term per line) and the standard terms in `.../DevStandard/accept.txt`. A person adds terms.
- Suppress a rule for a passage, for example a quoted banned word or vendored text, between
  `<!-- vale off -->` and `<!-- vale on -->` in Markdown, or for one rule with
  `<!-- vale DevStandard.Pronouns = NO -->`. Keep the passage short and say why.
- The rules work offline: no `vale sync`, no downloaded packages. To add a style such as write-good
  or proselint, vendor it into `.vale/styles/` at a pinned version, list it under `BasedOnStyles`
  and note the version and source in the pull request; a person reviews it (guardrail file).
