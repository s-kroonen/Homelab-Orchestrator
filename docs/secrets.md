# Secrets at rest: the master key

Every credential the orchestrator stores is encrypted with one **master key**
(`orchestrator/vault.py`). A copy of the data on its own reads as noise: `connections.yaml`, its
backup copies, the database file, or an orchestrator backup.

| What | Where it is stored | How |
|---|---|---|
| Connection secrets (Proxmox and PBS tokens, Portainer and Traefik Manager keys, MQTT, SMTP and Home Assistant passwords, ntfy and webhook tokens) | `config/connections.yaml` and `config/connections-backups/` | encrypted |
| Deploy webhook keys | database, `setting` rows `deploy.webhook.<slug>` | encrypted |
| The push (VAPID) private key | database, `setting` row `push.vapid` | encrypted |
| The session-cookie key | nowhere: derived from the master key | derived |
| Assistant (MCP) tokens, invite tokens | database | hashed or single-use, as before |

A stored value looks like `enc:v1:3f9c1a2b7d4e:<nonce>:<ciphertext>`.
* **Algorithm:** AES-256-GCM, with a fresh nonce for every value.
* **Key id:** the second part (`3f9c1a2b7d4e`) is the key's id, a fingerprint. It is safe to
  show, and the only form in which the key ever appears in logs, the CLI or the dashboard.

`.env` is not covered yet. Secrets still set there (an install from before Connections) move
into `connections.yaml` with `orchestrator-cli connections` or from Sources (*Move into the file*), and
are encrypted there. Moving every setting out of `.env` is a later step.

## Where the key lives

The orchestrator uses the first of these that exists:

1. **The file `ORCHESTRATOR_MASTER_KEY_FILE` names.**
2. **A Docker secret at `/run/secrets/orchestrator_master_key`.** Recommended: it keeps the
   key out of the data volume.
3. **`STATE_DIR/keys/master.key`** (`/data/keys/master.key` in Docker). The orchestrator
   makes this one on first start: 32 random bytes as base64, mode `0400`, in a `0700` folder.

The key is never part of an orchestrator backup, a log line or an API answer.

### A Docker secret (recommended)

```bash
mkdir -p secrets
openssl rand -base64 32 > secrets/orchestrator_master_key
chmod 400 secrets/orchestrator_master_key
```

Then uncomment the two `secrets:` blocks in `docker-compose.yml`. `secrets/orchestrator_master_key*`
is git-ignored.

**Moving an existing key there:**
1. Write the output of `orchestrator-cli secrets export-key` to that file.
2. Remove `/data/keys/master.key`.
3. Restart.

## Back it up

```bash
docker compose exec orchestrator orchestrator-cli secrets export-key
```

This prints the key once (after a confirmation) and records that in the audit log. **Keep it
in a password manager.**

**Restoring on a new host** (an orchestrator backup, or a copied volume) **needs the key.**
1. Write it to a file.
2. Set `ORCHESTRATOR_MASTER_KEY_FILE` to that file, or use the Docker secret.

## When the key is missing or wrong

The database records which key its data is encrypted with (the `vault.key` setting). If that key
is not found, or a different one is, **the orchestrator refuses to start**. It never runs with
credentials it cannot read. The message names the key it needs:

```
The orchestrator will not start: the master key is missing: /data/keys/master.key does not
exist, but the data here was encrypted with key 3f9c1a2b7d4e. Put that key back ...
```

Put the right key back. Nothing is changed while it refuses.

## Commands

| Command | What it does |
|---|---|
| `orchestrator-cli secrets status` | The key id and its file; how many secrets are encrypted; any still plain |
| `orchestrator-cli secrets encrypt` | Encrypt everything still plain now. The orchestrator also does this a few minutes after every start. |
| `orchestrator-cli secrets decrypt` | Write everything back as plain text, before downgrading to an older version |
| `orchestrator-cli secrets rotate` | A new key, then everything re-encrypted with it |
| `orchestrator-cli secrets export-key` | Print the key once, for a password manager |

**Rotation:**
* The old key is kept as `master.key.previous` until everything is re-encrypted, so a crash
  halfway loses nothing. It is removed afterwards.
* A key that is a read-only Docker secret cannot be rotated in place: the command says how to
  do it by hand.
* After rotating, export the new key and replace the saved copy.
* If the session-cookie key is derived (no `SESSION_SECRET` set), everyone signs in again.

## Upgrading an existing install

Nothing to do.

**At the first start with this version**, the orchestrator makes the key and records its id
in the database (`vault.key_created` in the log, with the id). From then on, a missing key is
noticed. New secrets are encrypted at once.

**Five minutes later** (`SECRETS_ENCRYPT_DELAY_S`, 300 s), it encrypts the plain-text secrets
left from before: `connections.yaml`, its backup copies, deploy keys and the push key. Then it
vacuums the SQLite file, so the old plain-text values do not linger in free pages.

The wait matters for updates. An update that fails the updater's health check is rolled back
to the previous version, which cannot read encrypted values. By the time anything old is
rewritten, the update can no longer be rolled back.

Then run `secrets export-key` and save the key.

## Going back to an older version

A version from before encryption reads `enc:v1:…` as the credential itself, so every
connection would fail. Before downgrading:

```bash
docker compose exec orchestrator orchestrator-cli secrets decrypt
```

This writes every secret back as plain text and forgets the key id; the key file stays. Then
downgrade. If you later upgrade again, this version encrypts everything again after its delay.

## The setup container

`orchestrator-cli init` and `config edit` write `connections.yaml` in plain text. The setup
container has no access to the orchestrator's key, and the orchestrator encrypts the file after
its first start.

**Run `config edit` inside the orchestrator container**
(`docker compose exec orchestrator …`) once the file is encrypted. The setup container cannot
read it.

## For developers

* Read secrets through the code that owns them (`connections/store.py`, `deploy/webhook.py`,
  `notify/push.py`). Each decrypts with `vault.decrypt`, and plain values pass through unchanged.
* New secrets in the database go in `vault.DB_SECRETS`, so `encrypt`, `status` and `rotate`
  find them. Store them with `vault.encrypt(...)`.
* `vault.derive(purpose)` gives a stable secret that is never stored (the session key uses it).
* Tests: each test gets its own key under its `STATE_DIR` (`tests/conftest.py` resets the vault).
