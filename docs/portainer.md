# Portainer: what it has, read live

The **Containers** page (Infrastructure → Containers, `/portainer`), `orchestrator-cli portainer ...`
and `/api/portainer` show what Portainer knows: environments, stacks, containers, logs and stats.

On the page the stacks are a table (filter, chips for running / stopped / partly running / not
linked, an environment and, with several Portainers, a Portainer picker). Click a stack to see
its containers inside it, each with its image, state, published ports, CPU and memory, **Logs**
(a drawer: lines, follow, timestamps, wrap, filter, download) and **Restart**, the rest in ⋯.
A stack's ⋯ has Start, Stop, Restart, Link to a service, Open in Portainer and Delete; select
several to start, stop or restart them together. **+ New stack** opens a drawer that previews the
exact request before anything is sent. Adopting, creating,
deleting and deploying stacks - the write side - is in
[portainer_deploys.md](portainer_deploys.md); this page is the read side.

Portainer owns stacks the way Proxmox owns guests. The orchestrator reads them and, later,
creates and deletes them only through Portainer's API. It copies nothing: no compose files, no
Git URLs or branches into `services.yaml`, no stack environment, no container lists. The one
thing it keeps is the *link* from a sub-service to a stack (a `controller:` with the
environment and stack name) - and this page tells you which stacks and containers already have
one (the Service column).

## Setup

Add Portainer on the **Connections** page (or `orchestrator-cli connections add portainer --set
url=https://portainer.example.lan:9443 --set api_key=ptr_...`) - no restart. You can have
several; the pages and the CLI take the one to look at (`-i NAME`), and a controller or probe
names its connection with `instance:` (see [connections.md](connections.md)).

| Field | |
|---|---|
| `url` | Portainer itself, `https://<host>:9443` |
| `api_key` | Portainer: My account -> Access tokens |
| `verify_tls` | `false` for a self-signed certificate |
| `public_url` | what you open in a browser, if different (for "Open in Portainer") |
| `read_only` | every write refuses |

An install that still has `PORTAINER_URL` and `PORTAINER_API_KEY` in `.env` keeps working; move it
into the file from the Connections page.

The token acts as the user that created it, so for reading only a user with **read** access to
the environments is enough; the write side ([portainer_deploys.md](portainer_deploys.md)) needs
one that can manage stacks, and `PORTAINER_READ_ONLY=true` makes every write refuse. `orchestrator-cli check` and the Connectivity
page list Portainer (reachable, key accepted, version, edition, environments) and each
environment; an environment Portainer reports as down shows as a finding. Portainer not
configured is a note, not a failure.

## What is shown

The **Containers** page lists every stack on every Portainer connection (one "(no stack)" row per
environment for containers outside any stack). A row opens the **stack page**
(`/portainer/<environment>/<stack>`), built to the Stack and StackEnv wireframes:

* **Containers**: one row each (image, state, uptime, ports, CPU and memory, its check on the
  linked sub-service). Click one for its details (image, start, restart policy, volumes, networks,
  environment names, Docker healthcheck) and its live logs. Row menu: start, stop, restart,
  **kill**, **recreate** (pull its image and create it again), **inspect** (`docker inspect`,
  environment values masked), and its check: required, optional or off, and whether a backup of
  the parent waits for it (`backup_waits`, see [service_tree.md](service_tree.md)).
* **Logs**: every container's logs in one view, with a per-container filter.
* **Environment**: the stack's variables and which containers have them. A value whose name looks
  secret (PASSWORD, SECRET, TOKEN, KEY, DSN, AUTH, ...) is hidden until you click *show*, which is
  audited as `stack.env_revealed` (the name, never the value). **Edit** writes the whole list
  back to Portainer and **redeploys** the stack; a hidden value is kept unless you type a new one
  in its place. Audited as `stack.env_changed` with the names added, changed and removed.
* **Settings**: the Git source, branch and Portainer's own polling (editable: `stack.git_settings`),
  and what the orchestrator keeps: the linked sub-service, its deploy method and what a backup
  waits for.
* **History**: the stack's and its sub-service's runs and changes.
* Header: **Redeploy** (the sub-service's deploy, with its checks, when it has one; else a plain
  Portainer redeploy), Restart, Stop / Start, Open in Portainer, Delete.

In more detail:

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

* stack **environment values**, except on the stack page: there a value is shown unless its
  name looks secret, and a secret one only when someone clicks *show* (audited). Every other
  page, API response, log line and the CLI keep names only;
* a container's **environment** - names only (inspect masks every value);
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
endpoints/{id}/containers/{name-or-id}[/logs?tail=&since=&timestamps= | /stats | /inspect]`

The stack page also uses `GET /api/stacks/{env}/{name}/link` (what is linked, from the database),
`GET .../env`, `POST .../env/reveal {name}`, `PUT .../env {vars: [{name, value|null}]}` (null keeps
the current value; redeploys), `PUT .../git {reference, auto_update_interval}`,
`POST /api/stacks/{env}/{name}/redeploy` and `POST /api/containers/{env}/{ref}/kill|recreate`.
All take `?instance=` for a Portainer connection other than the default.

Portainer problems map to HTTP: not configured 503, key refused or unreachable 502, unknown
object 404.

## Notes

* Reads are cached for 5 seconds (a page load asks for the same list several times); logs and
  stats never are.
* Only Docker-type environments are read (Docker, Agent, Edge). Kubernetes and Azure ones
  are listed as environments but have no stacks here.
* The "Open in Portainer" link shapes follow Portainer 2.x; the Connectivity check reports
  the version, and the shapes are the first thing to check if a link lands on the wrong page.
* **Unreachable environments are not shown.** Portainer keeps a stack's record, still "active",
  after its environment goes away (an agent that is down, a guest that is off), so listing it
  would claim a stack is running that nobody can reach. An environment Portainer reports as down,
  or whose containers cannot be read (a 5xx, a timeout), is left out of the page, the environment
  list and the stack lists (page, Data sources, CLI), and one line names what was hidden. The other
  environments load normally. It comes back by itself once it answers again.
* **Point the connection's `url` at Portainer itself** (`https://<host>:9443`), not at the public
  address behind Traefik. Behind Traefik, a 5xx from Portainer can be replaced by the
  orchestrator's own "not available" page, which hides Portainer's real message; the error
  then reads "an HTML page came back instead of Portainer's reply". `PORTAINER_PUBLIC_URL` is
  only for the "Open in Portainer" links.
