# The host runner

Some checks need credentials: an Ansible inventory, playbooks, an SSH key. The
orchestrator also serves a web listener. Putting those two things in the same
container means a container compromise hands over the keys to every guest.

The host runner separates them:

```
   container                    host
  ┌───────────────┐            ┌──────────────────────────┐
  │ orchestrator  │            │ orchestrator-host-runner │
  │               │──socket──▶ │                          │──ansible/ssh──▶ guests
  │ no keys       │            │ owns inventory           │
  │ no inventory  │            │ owns playbooks           │
  │ no playbooks  │            │ owns the ssh key         │
  │ no ansible    │            │ enforces an allowlist    │
  └───────────────┘            └──────────────────────────┘
```

The container asks for a check **by name**. The runner decides whether that is
allowed, and performs it. A socket is a *capability*, not a file tree: the
container can request the checks you approved and can read none of the material
behind them.

## What this actually buys you

If the container is compromised, the attacker gets:

* the ability to request checks **on your allowlist**, against hosts **in your
  allowlist** — no more
* **no** private key, **no** inventory, **no** playbook contents
* **no** arbitrary remote execution, unless you explicitly turned on ad-hoc
  commands (off by default, and the CLI warns when it sees it enabled)

Without it — mounting the inventory, playbooks and key into the container — the
attacker gets all of those immediately.

## Install

Everything below happens **on the host**. Nothing is added to the container.

```bash
sudo useradd --system --shell /usr/sbin/nologin orchestrator-runner
sudo groupadd -f orchestrator
getent group orchestrator      # note the gid — you need it below
```

That group is the gate: the socket is mode `0660`, owned by
`orchestrator-runner:orchestrator`, so only members of it can connect.

```bash
sudo install -m 0755 contrib/host-runner/orchestrator-host-runner /usr/local/bin/
sudo install -d -m 0750 /etc/orchestrator
sudo install -m 0640 contrib/host-runner/host-runner.example.yaml \
    /etc/orchestrator/host-runner.yaml
sudo chown -R orchestrator-runner:orchestrator /etc/orchestrator
```

Edit `/etc/orchestrator/host-runner.yaml` — it is the allowlist, and the only
place that decides what the container may ask for.

```bash
sudo install -m 0644 contrib/host-runner/orchestrator-host-runner.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orchestrator-host-runner
sudo systemctl status orchestrator-host-runner
```

The runner is **stdlib-only** — no venv, no pip, nothing to keep patched. It is
also short enough to read end to end before you trust it, which is the point of
a component whose whole job is holding a privilege boundary.

### The runner's own access

Grant `orchestrator-runner` only what these checks need:

* read on the inventory and playbook directory
* an SSH key whose authorized use on the guests is as narrow as you can make it
  (a `command=` restriction in `authorized_keys` is worth the effort)

It deliberately does **not** run as root. The systemd unit adds
`ProtectSystem=strict`, `NoNewPrivileges` and friends.

## Wiring it up

Mount the socket — and only the socket:

```yaml
    volumes:
      - /run/orchestrator/host-runner.sock:/run/orchestrator/host-runner.sock
    group_add:
      - "1001"             # the HOST gid from `getent group orchestrator`
```

**Use the numeric gid, not the name.** `group_add` resolves names against the
*container's* `/etc/group`, and this image already defines its own
`orchestrator` group at gid 1000 — so the name would quietly resolve to the
wrong group and every connection would get `Permission denied`. The kernel
checks the number.

Confirm the container actually landed in the host group:

```bash
docker compose exec orchestrator id
```

```bash
# .env
HOST_RUNNER_SOCKET=/run/orchestrator/host-runner.sock
```

Then confirm it:

```bash
docker compose exec orchestrator orchestrator-cli transport check
```

That reports whether the runner answered and **what it will allow**, so the
allowlist is visible from the orchestrator side without granting any access to
it. Add `--host <inventory-host>` to run a real ping through the runner.

## Using it in probes

```bash
orchestrator-cli probe add haos --kind ansible_ping
```

```yaml
      - name: ansible-ping
        kind: ansible_ping
        required: true
        config:
          transport:
            type: host_agent
            host: haos          # an inventory host or group
            action: ping
```

Any command probe can route the same way:

```yaml
      - name: docker
        kind: docker_project
        config:
          project: media-stack
          transport:
            type: host_agent
            host: media-vm
```

And a playbook, **by name** — the runner resolves it against its own
`playbook_dir`, so the container never chooses a filesystem path:

```yaml
      - name: db-integrity
        kind: ansible_playbook
        config:
          playbook: check-mariadb.yml     # must be in allow.playbooks
          transport:
            type: host_agent
            host: db-vm
            action: playbook
```

`type: ansible` is accepted as an alias for `host_agent`, since that is how you
probably think about it — but execution is always on the host.

## Multiple hosts on one playbook

**One probe produces one verdict for one service.** A run spanning many hosts
collapses them into a single exit code, so an unrelated host failing would
condemn this service. The runner therefore scopes each request with `--limit`.

Share the *playbook*, scope the *run*:

```yaml
  # service "cloud"
          playbook: check-db.yml
          transport: {type: host_agent, host: cloud-vm, action: playbook}

  # service "wiki" — same playbook, different host
          playbook: check-db.yml
          transport: {type: host_agent, host: wiki-vm, action: playbook}
```

One check to maintain; each service judged on its own. If a service genuinely
depends on a whole group being healthy, point `host` at the group and accept the
aggregate — but only where "any member down means this service is degraded" is
actually true.

## Exit-code semantics

Ansible distinguishes the two cases the gate depends on, and the runner passes
that through:

| Ansible exit | Meaning | Verdict |
|---|---|---|
| `0` | succeeded | **HEALTHY** |
| `2` | task failed — the command returned non-zero | **FAILED** (a real answer) |
| `4` | host unreachable | **UNKNOWN** (we could not look) |

The runner also reads the command's *own* rc from Ansible's JSON callback, so a
probe that needs to know it got 3 rather than 1 gets the real number.

A fourth case is specific to this design: if the runner **refuses** a request
(playbook not allowlisted, host out of scope), that is a configuration problem
on the orchestrator's side, not evidence about the service. It surfaces as
UNKNOWN with the refusal quoted — never FAILED, because a too-narrow allowlist
must not mark healthy services as corrupt.

## If you would rather not run it

The runner is optional. Without it:

* `http`, `tcp` and `mqtt` probes work unchanged — they need no credentials
* command probes can use `type: ssh`, which needs a key mounted into the
  container and brings back the exposure this exists to avoid

For a lab where the container is not reachable from anywhere interesting, that
tradeoff may be fine. For anything serving a listener you care about, the runner
is the better shape.
