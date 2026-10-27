# Connections

A *connection* is one system the orchestrator talks to: a Proxmox cluster, a Proxmox Backup
Server, Traefik Manager, a Portainer, Home Assistant, an MQTT broker, an alert channel, the SSH
settings. They used to be `.env` keys, one of each. They now live in **`config/connections.yaml`**,
which you edit from the dashboard (**Connections**) or with `orchestrator-cli connections`, and
which applies **without a restart**. A kind can exist more than once where that makes sense
(today: Portainer), so two Portainers are two connections.

`.env` keeps what belongs to the orchestrator itself: log level, dry run, ports, retention, the
session secret, the dashboard's hostname.

## Where the file is

`config/connections.yaml`, next to `services.yaml` (the directory every container mounts, so the
`setup` container can write it too; `CONNECTIONS_PATH` points it somewhere else). It holds
credentials: it is written readable by its owner only, git-ignored, and the last five copies are
kept in `config/connections-backups/`. Like `.env` it is **not** part of the System page's
backups (those can be downloaded); back it up with the rest of `config/` on the host.

```yaml
version: 1
connections:
  - id: home
    kind: portainer
    name: Portainer (home)
    enabled: true
    default: true
    site: home
    scope: local
    settings: {url: "https://10.0.0.5:9443", verify_tls: false}
    secrets: {api_key: ptr_...}
  - id: offsite
    kind: portainer
    site: offsite
    scope: shared
    settings: {url: "https://portainer.offsite.example:9443"}
    secrets: {api_key: ptr_...}
```

Edit it by hand if you like: the running orchestrator notices the change within seconds. A file
that cannot be read does not take anything down — the error is shown on the Connections page and
the previous settings stay in force.

## The page

**Connections** lists every kind with its connections, a **Test** button each (read-only: it
logs in, lists something, changes nothing; in dry-run mode nothing is contacted), and **Add** /
**Edit** forms built from the kind's fields. Saving applies at once: the adapters are rebuilt and
swapped in behind the same handles, so running schedulers and collectors pick up the new values.

* **Secrets are never shown again.** A saved secret reads "set — leave blank to keep"; typing in
  the box replaces it.
* **Only one of each kind may be switched on**, except Portainer. Turn the old one off first.
* **Make default** (Portainer): the connection that is used when a controller or probe names none.
  Making another one the default first pins the links that named none to the old default so they
  do not move; that changes the registry, so **Save to YAML** afterwards.
* **Delete** refuses while a service's controller or probe uses the connection (delete anyway
  with the confirmation, and those services stop working until you point them elsewhere).
* A **Controllers** table lists every service's controller and the connection it acts through.

## Kinds

| Kind | What it is | Fields (\* required, secret in *italics*) |
|---|---|---|
| `proxmox` | Proxmox VE API | host\*, port, verify_tls, token_id\*, *token_secret*\*, restore_token_id, *restore_token_secret*, backup_storage |
| `pbs` | Proxmox Backup Server | host\*, port, verify_tls, token_id\*, *token_secret*\*, datastore, node_name, protect_enabled |
| `proxy` | Traefik Manager (+ the probe address) | url\*, *api_key*\*, verify_tls, timeout_s, default_entrypoints, default_cert_resolver, redirect_middleware, hsts_middleware, config_file, probe_base_url |
| `traefik_api` | Traefik API (traffic metrics) | url\*, verify_tls, username, *password*, timeout_s |
| `power` | Power manager (Home Assistant MQTT) | host\*, port, tls, username, *password*, topic_prefix |
| `portainer` | Portainer (several allowed) | url\*, *api_key*\*, verify_tls, read_only, public_url |
| `haos` | Home Assistant (add-ons) | url\*, *token*\*, verify_tls |
| `ntfy` / `webhook` | alert channels | url\*, *token* |
| `email` | alert channel (SMTP) | host\*, port, starttls, username, *password*, sender, to\* |
| `ssh` | SSH, jump host, host runner | key_path, known_hosts_path, verify_host_key, jump_host, jump_user, jump_port, host_runner_socket |

Every field maps to the same setting the old `.env` key filled, so `orchestrator-cli connections
kinds` shows the exact names. A connection that is switched on and filled in makes its adapter
live; a kind with no connection stays on its dry-run adapter.

## Portainer: more than one

A controller (`{type: portainer, stack: media}`) and a Portainer probe may carry
`instance: <connection id>`. Without it they use the **default** connection, so existing links keep
working. The Portainer page, Data sources, the CLI (`orchestrator-cli portainer -i offsite ...`)
and MCP all take the connection to look at. See [portainer.md](portainer.md).

## Moving over from `.env`

Nothing breaks on upgrade: a kind that is **not** in the file keeps being read from `.env` (the
Connections page shows those as *from .env*). Once a kind is in the file, the file owns it and its
`.env` keys are ignored.

* **Move them**: the *Move them into the file* button (or `orchestrator-cli connections
  import-env`) writes every connection that only exists in `.env` into the file and lists the keys
  you can then delete from `.env` (the running container cannot edit your host `.env`).
* **Adding a second Portainer** moves the first one automatically, so it does not vanish.
* **The setup wizard** (`orchestrator-cli init`, `/setup`, `config edit <section>`) asks the same
  questions as before but writes the adapter answers to `config/connections.yaml`; anything the
  old `.env` still held for them is moved on the next save. A connector that is half filled in
  (a host without its token) is kept but switched off.

## Command line

```
orchestrator-cli connections list | show ID | kinds
orchestrator-cli connections add portainer --id offsite --set url=https://... --set api_key=... [--shared --site offsite]
orchestrator-cli connections set ID --set url=https://new --enable/--disable --clear api_key
orchestrator-cli connections default ID
orchestrator-cli connections test ID
orchestrator-cli connections remove ID [--force]
orchestrator-cli connections import-env
orchestrator-cli connections export [--with-secrets] [--all]  /  import FILE
```

It edits the file directly (run it in the container or the `setup` service); the running
orchestrator applies it by itself.

## API

All admin-only and audited (which fields changed, never their values):
`GET/POST /api/connections`, `PUT/DELETE /api/connections/{id}`, `POST /api/connections/{id}/test`,
`POST /api/connections/test` (a form not yet saved), `POST /api/connections/{id}/default`,
`POST /api/connections/import-env`, `POST /api/connections/reload`,
`GET /api/connections/controllers`, `GET /api/connections/export`, `POST /api/connections/import`.

## Sharing with another orchestrator

`site` says where a connection lives and `scope` is `local` (only reachable from this
orchestrator) or `shared` (the other site should have it too). **Export** prints the shared ones
without secrets and switched off; **Import** adds the ones this orchestrator lacks (ids already
here are left alone) and marks them shared. See [redundancy.md](redundancy.md) for where this is
heading.
