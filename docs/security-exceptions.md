# Security exceptions

One register for every accepted finding. Each scanner has its own ignore file; they all point
here, so the reason lives in one place. Review on the end date, or when the dependency changes.

| ID | Finding | Tool entries | Why it does not apply | Owner | Until |
|---|---|---|---|---|---|
| E-000 | Secret-like examples inside vendored rules (`.opengrep/rules/open/`) | `.gitleaks.toml` (path) | Rule files describe secret formats; they are not credentials | maintainer | — |
| E-001 | *(example)* `@grpc/grpc-js` CVE-2026-101916 / GHSA-m9gg-hp2v-232j | `.trivyignore.yaml`, `osv-scanner.toml` | Pulled in by firebase/firestore; the site never imports Firestore and grpc is absent from the bundle | <name> | 2027-02-01 |
| E-002 | *(example)* Firebase web API key in `lib/firebase_options.dart` | `.gitleaks.toml` (path + rule), `.semgrepignore` | Public client config by design; access is controlled by Firebase rules | <name> | — |

Rules:

- Match by **path + rule** or by **advisory id**, never by commit fingerprint: those break when history
  or the clone changes.
- No exception without an end date, except public-by-design values (E-002 type).
- An exception is a guardrail change: a human adds it in a reviewed PR.
