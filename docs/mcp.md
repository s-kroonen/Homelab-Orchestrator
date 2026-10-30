# MCP server: an assistant's eye on the server space

The orchestrator is an [MCP](https://modelcontextprotocol.io) server. Connect Claude (claude.ai,
Claude Desktop, mobile, Claude Code) and it can look at your services, health, backups, runs,
Portainer and nodes, and run a few commands (backups, deploys, wakes, start / stop) -
with the permissions **you** give each connection, and a **person approving anything dangerous**.

Everything here is also on the **AI assistants** page (`/assistants`) and in
`orchestrator-cli mcp`.

## Connect Claude

1. Make sure Claude can reach the dashboard over **https**: set `EXTERNAL_URL` (or
   `WEBAUTHN_ORIGIN`) to the public address, e.g. `https://orchestrator.example.com`. Anthropic's
   egress addresses (`160.79.104.0/21`) must be allowed through your firewall / WAF.
2. In claude.ai: *Settings → Connectors → Add custom connector*, paste
   `https://orchestrator.example.com/mcp`. In Claude Code:
   `claude mcp add --transport http homelab https://orchestrator.example.com/mcp`.
3. Claude opens the sign-in page. Log in with your passkey if asked, check **where it is
   sending you back to**, pick a **role** (or tick permissions), optionally *Ask me before every
   change*, and press **Allow**.

The page `/assistants` (Settings → AI assistants) lists the connection. Its ⋯ menu has **Change
role…** (a role, or ticked permissions, for the very next call), *Ask before every change*, **Its
calls** and **Revoke…**, which stops it at once. Requests waiting for you are at the top of the
page, with Deny and Review and approve.

For a client that cannot do OAuth (a script, the MCP inspector) make a **personal token** (**+ New token** on the
page) or with `orchestrator-cli mcp token NAME --role operator`. It is shown once; send it as
`Authorization: Bearer <token>`. Tokens in a query string are never accepted.

## Roles and permissions

A connection carries a set of permissions; a role is a named preset.

| permission | allows |
|------------|--------|
| `read` | see services, health, backups, runs, activity, Portainer, nodes, proxy hosts |
| `backup` | start backups; *propose* a restore (a person still approves it) |
| `deploy` | run guarded deploys |
| `wake` | wake services and nodes |
| `control` | start / stop / restart services, stacks, containers; the one switch; start a guest |
| `configure` | change settings: adopt a Portainer stack, a service's dump and deploy settings, a stack's Git branch and Portainer polling, which service a proxy host belongs to |
| `propose` | **ask for** dangerous actions (below); each one still needs a person |

| role | permissions |
|------|-------------|
| `viewer` | read |
| `operator` | read, backup, wake |
| `deployer` | read, backup, wake, deploy, control |
| `admin` | all of the above, including configure and propose |

A connection made before `configure` existed does not have it: tick it on the AI assistants
page.

The consent page starts at `viewer`; nothing is granted you did not tick. A tool the connection
may not use is not even listed to the assistant.

## Tools

`orchestrator-cli mcp tools` (or the page) lists them all. Three kinds:

* **read**: `orchestrator_status`, `list_services`, `get_service`, `service_health_history`,
  `linked_state`, `recent_activity`, `list_backups`, `backup_plan`, `list_runs`, `get_run`,
  `deploy_history`, `list_dumps`, `infra_status`, `list_nodes`, `list_guests`, `list_proxy_hosts`,
  `portainer_stacks`, `portainer_containers`, `container_logs` (secrets redacted), `get_request`.
* **act** (runs at once, logged, audited as `mcp:<connection>`): `run_backup`, `run_dump`, `propose_restore`,
  `deploy_service` (with its guards), `wake_service`, `node_power wake`, `switch_service`,
  `service_action`, `stack_action`, `container_action`, `guest_power start`.
* **configure** (act, needs `configure`): `adopt_stack`, `set_dump`, `set_deploy`, `set_stack_git`,
  `link_proxy_host`. They make the same writes as the pages (validated, audited, `services.yaml`
  saved by itself). Secrets never pass through them: a deploy webhook's secret is made on the
  service page, and `adopt_stack` does not return one.
* **dangerous** (waits for a person, needs `propose`): `guest_power stop`, `node_power power_off` /
  `restart`, `delete_stack`, `deploy_service` with `skip_guards`, `switch_service off` with
  `include_guest`, `service_action stop` / `restart` on a `guest`, `set_dump` with a `command`
  (it runs a shell command in a container).

The tools call the same code the dashboard buttons do, so every guard applies unchanged: nothing
is stopped under a running backup, deploys run their pre-guards, the switch keeps its order.

## Dangerous requests need a human

A dangerous call does **not** run. The assistant gets *"NOT DONE"*, a `request_id` and a link, and
you get a notification (`mcp_request`, on by default) with the same link. Open it (logged in): it
shows exactly what will run and as whom. **Approve** runs that stored request once, right now;
**Deny** drops it. Requests expire after `MCP_APPROVAL_TTL_MIN` (60). Revoking the connection
cancels what it had waiting. The assistant can read the outcome with `get_request`.

*Ask me before every change* puts every non-read call of that connection through the same step.

## History

Every call is recorded: when, which connection, tool, risk, redacted arguments, result and, for
requests, who decided. The page shows the newest under **Recent calls**; **All calls** filters by connection, tool,
result and risk or with
`orchestrator-cli mcp history`. Changes also show in **Activity** as `mcp.*` / the usual action
entries under the actor `mcp:<connection>`.

## How the sign-in works

Standard OAuth 2.1 as Claude's connectors expect it:

* `POST /mcp` without a valid token answers **401** with
  `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp"`.
* `/.well-known/oauth-protected-resource[/mcp]` (RFC 9728) names this server as its own
  authorization server; `/.well-known/oauth-authorization-server` (RFC 8414) advertises
  authorization code + **PKCE S256**, public clients (`none`), refresh tokens, dynamic client
  registration (`/oauth/register`, RFC 7591) and client ID metadata documents.
* Redirect URIs: `https://claude.ai/api/mcp/auth_callback` and the `claude.com` one, loopback
  `http://localhost|127.0.0.1[:any port]/…` for Claude Code, https elsewhere, an app's own
  scheme. The consent page shows the destination host and warns for anything that is not Claude's
  or is the connecting device itself.
* Client metadata documents (a client whose `client_id` is an https URL) are only fetched from
  `MCP_CIMD_HOSTS` (default `claude.ai,claude.com`), never from a private address.
* `/oauth/token` takes form-encoded requests; a refresh token is **rotated** on every use, and
  presenting an old one revokes the connection (a copy leaked). Tokens are stored hashed.

## Settings

| setting | default | |
|---------|---------|---|
| `MCP_ENABLED` | `true` | `false` makes `/mcp` and the OAuth endpoints answer 404 |
| `MCP_ACCESS_TTL_S` | `3600` | access token lifetime (clients refresh) |
| `MCP_REFRESH_TTL_DAYS` | `90` | a connection unused this long must be re-authorised |
| `MCP_APPROVAL_TTL_MIN` | `60` | how long a request waits for a person |
| `MCP_CIMD_HOSTS` | `claude.ai,claude.com` | hosts whose client metadata is fetched |

## Limits worth knowing

* The server is stateless (no MCP sessions, no server-to-client stream), so it works behind any
  proxy and survives restarts. It only answers `POST`.
* A backup or deploy runs in the server; the assistant gets a run id and polls `get_run`.
* Dry-run mode (`DRY_RUN=true`) applies: tools act on the simulated adapters.
