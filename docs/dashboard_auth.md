# Dashboard auth: passkeys, invites, sessions

Phase 7. The dashboard (`GET /`) and every `/api/registry/*`, `/api/backup/*`,
`/api/runs*`, `/api/backups`, and `/api/infra/*` endpoint require a logged-in
admin. `/healthz`, `/readyz`, `/maintenance/{slug}`, and `/wake/{slug}` stay
public **on purpose** — Traefik's `errors` middleware routes arbitrary,
unauthenticated visitors to the maintenance page during an outage (see
[architecture.md](architecture.md)), and gating that behind a login would
break the one feature it exists for.

## Why passkeys, no passwords

WebAuthn passkeys (`webauthn~=2.3`) only — no password/email flow. A passkey
is bound to this origin and can't be phished the way a password can, and it
removes an entire class of problem (password reset, password storage, reused
passwords) from a single-purpose homelab tool. `Admin.username` is purely an
identity label — it's what shows up in `audit_entry.actor` — not a secret.

## Bootstrap: the first admin

Registration (`POST /api/auth/register/begin`) needs no invite **only while
the `admin` table is empty**. The moment one admin exists, every further
registration needs a valid, unused, unexpired invite link (below). This is
why standing the dashboard up behind Traefik should happen only *after* you've
registered the first admin against `http://127.0.0.1:8080` (or over SSH port
forwarding) — a brief window where anyone who can reach the port could claim
the first admin slot is the tradeoff for not needing a CLI step or a
pre-shared secret. If you miss that window, see **Break-glass** below.

## Invites: adding more admins

Any logged-in admin can create one on Settings → Admins & devices ("Create
invite link"), or via `POST /api/auth/invites`. It returns a one-time URL:

```
{WEBAUTHN_ORIGIN}/login?invite=<token>
```

Send that to the person you're inviting. They open it, pick a username, and
register a passkey — no CLI access needed on their end. The link:

* **works once.** The invite's `PendingApproval` row (`kind=admin_invite`)
  moves from `pending` to `consumed` the moment registration completes, and a
  consumed or expired token is rejected with a 403.
* **expires** after `INVITE_TTL_HOURS` (default 24, see `.env`).
* stays copyable while it is open: the Invites list (`GET /api/auth/invites`,
  admins only) carries the link of a pending, unexpired invite and drops it
  once the invite is used or expires. Any admin can make a new link anyway, so
  this shows nothing an admin could not already get.
* reuses the existing generic `PendingApproval` table (the same one phase 8's
  revert/greenlight approvals will use) rather than a dedicated table — see
  the `ApprovalKind.ADMIN_INVITE` member in `domain/enums.py`.

## Adding a second device to your own account

Passkeys are per-device. To register a second one (a phone, a second laptop,
a hardware key) for an account you're *already* logged in as, use "Add this
device" on Settings → Admins & devices, or `POST /api/auth/credentials/add/begin` /
`/complete` directly — this does not consume or need an invite, since you're
already proving who you are via your current session. The list marks the
passkey this browser signed in (or was added) with as "this device"; the
session remembers its id for that, and for nothing else.

## Sessions

A signed cookie (Starlette's `SessionMiddleware`, keyed by `SESSION_SECRET`),
not a DB table — it holds the logged-in `admin_id` plus, briefly, the
in-flight WebAuthn challenge between a ceremony's `begin` and `complete`
calls. `same_site="lax"`.

**`https_only=False` is deliberate, not an oversight.** Traefik terminates
TLS at the gateway and speaks plain HTTP to this container over the internal
network (see [gateway_traefik_migration.md](gateway_traefik_migration.md)).
Starlette only sees that internal hop, which is never HTTPS from its point of
view — setting `https_only=True` here would make the cookie refuse to be set
at all on a correctly configured deployment. The browser-to-Traefik leg is
still HTTPS; only the Traefik-to-orchestrator leg inside your own network is
not, same as every other service behind this gateway.

## Break-glass: all passkeys lost

If every registered device is lost (or nobody ever completed the bootstrap
step and `WEBAUTHN_RP_ID`/`WEBAUTHN_ORIGIN` need fixing first), run:

```bash
orchestrator-cli admin reset
docker compose exec orchestrator orchestrator-cli admin reset   # in Docker
```

This lists every admin, asks for confirmation, then deletes all `admin` and
`webauthn_credential` rows. The CLI works without any of this (it's the
documented break-glass path, same reasoning as the host runner being usable
when the dashboard itself is down) — afterward, the zero-admin bootstrap rule
reopens and the next visitor to `/login` can register a fresh first admin.

## Audit trail

Every auth-relevant action writes an `audit_entry` row via
`orchestrator.audit.log.record` — `auth.login` (OK or DENIED),
`auth.logout`, `auth.register`, `auth.credential.added`,
`auth.invite.created` — with `actor` set to the admin's username (or
`"unknown"` for a login attempt against a credential that doesn't exist).
