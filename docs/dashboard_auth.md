# Dashboard auth: passkeys, invites, sessions

Phase 7. The dashboard (`GET /`) and every `/api/registry/*`, `/api/backup/*`,
`/api/runs*`, `/api/backups`, and `/api/infra/*` endpoint require a logged-in
admin. `/healthz`, `/readyz` and `/maintenance/{slug}` stay public **on purpose** —
Traefik's `errors` middleware routes arbitrary, unauthenticated visitors to the maintenance
page during an outage (see [architecture.md](architecture.md)), and gating that behind a login
would break the one feature it exists for. The maintenance page only wakes a service whose
"wake on hit" switches are on (and, for a button service, when the button was pressed).
`/wake/{slug}`, which skips those switches, is for admins only, and so is the API reference
(`/docs`, over `/api/openapi.json`); FastAPI's public `/docs`, `/redoc` and `/openapi.json`
are switched off.

## Requests from other sites

Every `POST`, `PUT`, `PATCH` and `DELETE` must come from the dashboard itself
(`web/same_origin.py`): its `Origin` must be the host it was sent to or the host of
`EXTERNAL_URL`, `WEBAUTHN_ORIGIN` or `ORCHESTRATOR_PUBLIC_URL`, and without an `Origin` its
`Sec-Fetch-Site` must be `same-origin`. Anything else is answered `403`. The session cookie's
`SameSite=lax` alone does not stop this: another app on a sibling subdomain counts as the same
*site*, so a script-injection bug in one of them could otherwise make a signed-in browser act
on the dashboard. Requests with neither header (the CLI, curl, a server) are not browsers and
pass as before; the token-authenticated paths (`/hooks/`, `/mcp`, `/oauth/token`,
`/oauth/revoke`, `/oauth/register`) are left to their tokens.

## Why passkeys, no passwords

WebAuthn passkeys (`webauthn~=2.3`) only — no password/email flow. A passkey
is bound to this origin and can't be phished the way a password can, and it
removes an entire class of problem (password reset, password storage, reused
passwords) from a single-purpose homelab tool. `Admin.username` is purely an
identity label — it's what shows up in `audit_entry.actor` — not a secret.

## Bootstrap: the first admin

Registration (`POST /api/auth/register/begin`) always needs an invite link, **also while no
admin exists**. The dashboard is usually on the internet, so an open first-admin signup would
make whoever loads `/login` first the admin.

The first admin registers with the **setup link** instead:

* **At start.** While no admin exists, the orchestrator prints the link in its log, valid for
  `INVITE_TTL_HOURS`. A restart prints the same link until it expires.

  ```bash
  docker compose logs orchestrator | grep setup_link
  ```

* **On the host.** `orchestrator-cli admin invite` prints a fresh one, valid 30 minutes.

Without the link, `/login` says where to find it, and `register/begin` answers 403. The setup
link is an ordinary invite (below) with `setup: true` in its payload: it works once, and it
shows in the Invites list afterwards.

## Invites: adding more admins

Any logged-in admin can create one on Settings → Admins & devices ("Create
invite link"), or via `POST /api/auth/invites`. On the host,
`orchestrator-cli admin invite [--label X] [--minutes N]` does the same. It returns a
one-time URL:

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
passkey this browser signed in (or was added) with as "this device".

**Lost every device, but the host is reachable?** `orchestrator-cli admin invite --for NAME`
prints a link that adds a passkey to the existing account `NAME`: same username, same
history, nothing removed. The login page shows "Add a passkey to NAME" for it.

## Managing passkeys, admins and invites

On Settings → Admins & devices:

* **Your passkeys:** ⋯ → *Rename…* or *Remove…*.
  * Removing one ends every session it signed in, and that device can no longer sign in.
  * Removing the one this browser uses signs you out here.
  * Your **only** passkey cannot be removed: add another device first.
* **Invites:** ⋯ → *Revoke…* on an open link. It stops working at once and shows as *revoked*.
* **Admins:** ⋯ on another admin → *Disable…* / *Enable* or *Remove…* (type the name to confirm). Your own row has no ⋯.
  * `POST /api/auth/admins/{id}/disable` stops them signing in and ends every session they
    have. It raises their `session_version`, so even after `/enable` they sign in afresh.
  * `DELETE /api/auth/admins/{id}` removes them and their passkeys.
  * You cannot disable or remove yourself. Since you are active, that keeps at least one
    active admin.

| Action | API |
|---|---|
| Rename a passkey | `PATCH /api/auth/credentials/{id}` `{"label": "…"}` |
| Remove a passkey | `DELETE /api/auth/credentials/{id}` |
| List admins | `GET /api/auth/admins` |
| Disable / enable | `POST /api/auth/admins/{id}/disable` · `/enable` |
| Remove an admin | `DELETE /api/auth/admins/{id}` |
| Revoke an invite | `DELETE /api/auth/invites/{id}` |

On the host: `orchestrator-cli admin list`, `admin disable NAME`, `admin enable NAME`. The CLI
refuses to disable the last active admin.

## Sessions

A signed cookie (Starlette's `SessionMiddleware`, keyed by `SESSION_SECRET`),
not a DB table. It holds:

* the logged-in `admin_id` and the admin's `session_version` at sign-in;
* the passkey it signed in with: its row id and a hash of its WebAuthn id;
* briefly, the in-flight WebAuthn challenge between a ceremony's `begin` and `complete` calls.

`same_site="lax"`.

**A session lasts only as long as its passkey, and until its admin is disabled.** Every request
checks that the passkey still exists and still belongs to that admin, and that the admin's
`session_version` is still the one it signed in with. So removing a passkey ends every session it signed in,
and `admin reset` ends them all. The hash matters: once a table is empty, SQLite hands out the
same row ids again, so after a reset the new first admin is admin 1 with passkey 1, exactly
what an old cookie names. Cookies from before this check carry no hash and must sign in again
once.

**`https_only=False` is deliberate, not an oversight.** Traefik terminates
TLS at the gateway and speaks plain HTTP to this container over the internal
network (see [gateway_traefik_migration.md](gateway_traefik_migration.md)).
Starlette only sees that internal hop, which is never HTTPS from its point of
view — setting `https_only=True` here would make the cookie refuse to be set
at all on a correctly configured deployment. The browser-to-Traefik leg is
still HTTPS; only the Traefik-to-orchestrator leg inside your own network is
not, same as every other service behind this gateway.

## Break-glass: from the host

The CLI works without the dashboard, so these are the ways back in when nobody can sign in.
In Docker, prefix each with `docker compose exec orchestrator`.

| Situation | Command |
|---|---|
| One admin lost their devices | `orchestrator-cli admin invite --for NAME` |
| A device was stolen | `orchestrator-cli admin passkeys`, then `admin passkeys --remove ID` |
| An admin must be locked out | `orchestrator-cli admin disable NAME` |
| Every passkey of every admin is lost | `orchestrator-cli admin reset` |

* **`admin passkeys`** lists every passkey (id, admin, device, added, last used).
  `--remove ID` deletes one and ends the sessions it signed in. If that was the admin's last
  passkey, it says how to add one.
* **`admin reset`** lists every admin, asks for confirmation, then:
  * deletes all `admin` and `webauthn_credential` rows;
  * ends every session (see **Sessions**);
  * expires every open invite, so a link handed out earlier cannot become the way in;
  * prints a setup link, valid 30 minutes. Registration stays closed without it.

## Audit trail

Every auth-relevant action writes an `audit_entry` row via
`orchestrator.audit.log.record` — `auth.login` (OK or DENIED),
`auth.logout`, `auth.register`, `auth.credential.added`, `auth.credential.renamed`,
`auth.credential.removed`, `auth.admin.disabled`, `auth.admin.enabled`, `auth.admin.removed`,
`auth.invite.created`, `auth.invite.revoked`, `auth.reset` — with `actor` set to the admin's username (or
`"unknown"` for a login attempt against a credential that doesn't exist, and `"cli"` for
the host commands).
