# Control surface security — Homelab Orchestrator

Requirements for every interface that can change state (REST, WebSocket, LAN discovery, desktop
bridge). From constitution principle "Secure control surface". Each requirement gets a test; specs
that touch the control surface link the IDs here in their **Security & Data** section.

| ID | Requirement | Test |
|---|---|---|
| CS-1 | Every state-changing endpoint and every WebSocket requires an authenticated session | <test name> |
| CS-2 | New clients are paired explicitly (one-time code on the host, revocable list of paired clients) | |
| CS-3 | The service binds to localhost by default; LAN exposure is an explicit setting, shown in the UI | |
| CS-4 | Browser requests: `Origin` and `Host` are checked against an allow-list (no CSRF, no DNS rebinding); WebSocket upgrades too | |
| CS-5 | Input is validated at the boundary (types, ranges, sizes); unknown fields are rejected | |
| CS-6 | Rate limits on login / pairing | |
| CS-7 | No secrets in logs; tokens stored hashed | |

Threats this covers (from the review of the previous version): unauthenticated control of fixtures
from the LAN, cross-site requests from a web page to a local server, WebSocket hijacking.
