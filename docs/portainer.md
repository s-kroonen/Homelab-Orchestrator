# Portainer: what it has, read live

The **Portainer** page (`/portainer`), `orchestrator-cli portainer ...` and `/api/portainer`
show what Portainer knows: environments, stacks, containers, logs and stats. Adopting, creating,
deleting and deploying stacks - the write side - is in
[portainer_deploys.md](portainer_deploys.md); this page is the read side.

Portainer owns stacks the way Proxmox owns guests. The orchestrator reads them and, later,
creates and deletes them only through Portainer's API. It copies nothing: no compose files, no
Git URLs or branches into `services.yaml`, no stack environment, no container lists. The one
thing it keeps is the *link* from a sub-service to a stack (a `controller:` with the
environment and stack name) - and this page tells you which stacks and containers already have
one ("Linked to").

## Setup

```bash
PORTAINER_URL=https://portainer.example.lan:9443
PORTAINER_API_KEY=ptr_...          # Portainer: My account -> Access tokens
PORTAINER_VERIFY_TLS=true          # false for a self-signed certificate
PORTAINER_PUBLIC_URL=              # what you open in a browser, if different (for "Open in Portainer")
PORTAINER_ADAPTER=api              # dry_run serves sample data (DRY_RUN=true does too)
```

The token acts as the user that created it, so for reading only a user with **read** access to
the environments is enough; the write side ([portainer_deploys.md](portainer_deploys.md)) needs
one that can manage stacks, and `PORTAINER_READ_ONLY=true` makes every write refuse. `orchestrator-cli check` and the Connectivity
page list Portainer (reachable, key accepted, version, edition, environments) and each
environment; an environment Portainer reports as down shows as a finding. Portainer not
configured is a note, not a failure.

## What is shown

* **Stacks**, per environment: status, **kind** (`image` pulls images, `build` builds from
  source, `mixed`; read from the compose file, which is parsed and dropped), containers running
  of total, the Git repository / branch / deployed commit, last update, and which sub-service
  links to it. Compose projects Portainer did not create (it only sees their containers) are
  listed too, flagged *not in Portainer's stack list*.
* **Containers** of a stack: state, health, image, published ports, restart count, and the
  sub-service linked to it.
* **Logs**: tail 100-2000 lines, follow (refreshes every 3 s), timestamps, wrap, filter,
  download. stderr is red. `orchestrator-cli portainer logs NAME -e 1 -f` does the same.
* **Open in Portainer** for every environment, stack and container, and "the same by hand":
  the `docker` / `docker compose` commands that show the same thing on the host.

## What is never shown

Portainer's API returns more than is safe to display. Everything is redacted at the client,
before any page, API response, log line or CLI output can contain it:

* stack **environment values** - only the variable *names* are kept;
* a container's **environment** - names only;
* **Git credentials**: user:token@ in a repository URL is stripped, the stored authentication is
  dropped, and only "uses credentials" remains;
* the stack's **webhook id** (it is a capability to redeploy): only "has a deploy webhook".

Tests check this by running a fake Portainer full of sentinel secrets and asserting none of
them appears in any response.

## CLI

```bash
orchestrator-cli portainer status
orchestrator-cli portainer environments
orchestrator-cli portainer stacks [-e ENV]
orchestrator-cli portainer containers -e ENV [-s STACK] [--running]
orchestrator-cli portainer logs CONTAINER -e ENV [-n 100] [-f]
orchestrator-cli portainer stats CONTAINER -e ENV
```

All accept `--json` where they list things. They talk to Portainer directly - they work
while the dashboard is down.

## API (admin login)

`GET /api/portainer/status | environments | stacks[?endpoint=] |
endpoints/{id}/stacks/{name} | endpoints/{id}/containers[?stack=&all=] |
endpoints/{id}/containers/{name-or-id}[/logs?tail=&since=&timestamps= | /stats]`

Portainer problems map to HTTP: not configured 503, key refused or unreachable 502, unknown
object 404.

## Notes

* Reads are cached for 5 seconds (a page load asks for the same list several times); logs and
  stats never are.
* Only Docker-type environments are read (Docker, Agent, Edge). Kubernetes and Azure ones
  are listed as environments but have no stacks here.
* The "Open in Portainer" link shapes follow Portainer 2.x; the Connectivity check reports
  the version, and the shapes are the first thing to check if a link lands on the wrong page.
