# Plan: managing Portainer stacks and containers, deploys and guards

Goal: everything about one deployed application - its Portainer stack, its containers
(including the database), its proxy host(s) and its deploy pipeline - becomes **one
sub-service** in the orchestrator, with one enable/disable switch, one place to monitor it,
one place to debug it, and a deploy that can be guarded by backups and probes. All of it
from the UI **and** the CLI.

The running example is `dev.xymbols`: a Portainer stack with its own proxy host, database
and Git-webhook deploy.

## Principles (these decide most of the choices below)

1. **Portainer owns stacks, the way Proxmox owns guests.** The orchestrator *reads* what
   exists in Portainer, and *creates and deletes only through Portainer's API*. It holds no
   information Portainer already has: no compose files, no Git URLs or branches, no stack
   environment, no container lists. A sub-service only *links* to a stack (Portainer
   instance + environment + stack name), exactly as a service links to a guest by VMID.
   What the orchestrator does keep, in `services.yaml` or its database, is what Portainer
   cannot hold: the links, the guard and disable policies, and run history.
2. **Observed state is what is shown; desired state is only intent.** On, off, or
   mixed is read from the real thing (the proxy, the guest, Portainer). Outside changes are
   shown and recorded, never silently reverted (see "Enable and disable").
3. **Every adapter has other control points.** Anything done here can still be done in
   Portainer, Proxmox or on the host. The UI links to the object there and shows the host
   command that does the same by hand.
4. **Guards are optional, per application.** A simple pipeline needs none and stays a
   pass-through; a guarded one can refuse or react to a deploy.
5. **Secrets stay in Portainer.** Stack environment values are never displayed, logged or
   stored by the orchestrator. Portainer's API returns them, so every read is *redacted at the
   client*; values typed into a create form are sent through to Portainer and not kept.
6. **Deploy triggers are pluggable.** The Git webhook pointing at the orchestrator is one
   option; Portainer's own webhook keeps working; GitLab pipelines are a later option. The
   guards do not care which one started the deploy.

## What exists today

* Sub-services (`parent`), each with probes, `controller:`, `recovery:`, `backup_gate:`, and
  an `enabled` flag. Disabling a service already **parks its subtree**: its proxy hosts stop
  being served and the scanner skips it.
* A Portainer **controller**: start / stop / restart of one container or one stack through
  `PORTAINER_URL` + `PORTAINER_API_KEY`.
* Proxy hosts linked to a service (`service_id`, editable on the proxy host form).
* Pipelines with run history (`PipelineRun`) shown in Activity, notifications and push.

Missing: discovery of what is in Portainer, richer state (containers, health, Git ref,
last update), logs, create/delete of stacks, redeploy, a deploy method, guards, enable/disable
that really reaches every linked thing, the guided link-up, and the CLI for all of it.

## The model

```yaml
services:
  - slug: portainer-vm            # the guest Portainer runs on (exists)
  - slug: dev-xymbols             # NEW: a sub-service for the stack
    parent: portainer-vm
    controller: {type: portainer, endpoint: 2, stack: dev-xymbols}   # a link, nothing more
    deploy:                       # NEW: orchestrator-only: how it is deployed, what guards it
      trigger:
        webhook: {provider: generic}   # token generated, shown once; gitlab/github later
      guard:                      # all optional; none = pass-through
        pre:  {probes: true, backup: {max_age_h: 24, take_if_older: true}}
        post: {probes: true, timeout_s: 180, settle_s: 30}
      on_failure: notify          # notify | stop | restore_backup | redeploy_previous
    backup: {method: guest}       # guest (default) | dump (later, see "Backups")
    proxy_hosts: [{hostname: dev.xymbols.com, ...}]   # exists
  - slug: dev-xymbols-db          # optional child: the database container
    parent: dev-xymbols
    controller: {type: portainer, endpoint: 2, container: dev-xymbols-db-1}
    backup_gate: required         # exists: a backup of the guest waits for it
```

Everything under `controller:` is an identifier. Where the code lives, which branch, what
image - that is read from Portainer when shown, never copied here.

## Reading Portainer (new `PortainerClient`, used by everything)

One shared client (today each controller builds its own request), tolerant of Portainer
version differences (it reads `/api/system/version` first and records it), with short
in-memory caching and **redaction of environment values at the boundary**.

| Need | Portainer API (to be confirmed against your version) |
|---|---|
| environments | `GET /api/endpoints` |
| stacks (name, type, status, Git URL/ref/hash, webhook id, updated at) | `GET /api/stacks`, `GET /api/stacks/{id}` |
| compose file, to classify `image` vs `build` only | `GET /api/stacks/{id}/file` (parsed, shown as a label, never stored) |
| containers of a stack, state, health, image | Docker proxy `.../docker/containers/json` + label `com.docker.compose.project` |
| one container (health detail, restarts, published ports) | `.../containers/{id}/json` |
| logs / stats | `.../containers/{id}/logs?...`, `.../stats?stream=false` |
| start / stop a stack | `POST /api/stacks/{id}/start`, `/stop?endpointId=` |
| redeploy from Git | `PUT /api/stacks/{id}/git/redeploy?endpointId=` |
| create a stack from a Git repository (or compose text) | `POST /api/stacks/create/standalone/repository` / `/string` |
| delete a stack | `DELETE /api/stacks/{id}?endpointId=` |
| Portainer's own deploy webhook | `POST /api/stacks/webhooks/{webhookId}` |

Also added to the connectivity checks (`/infra`, CLI): "Portainer: reachable, API key
accepted, version X, N environments".

## Creating and deleting through Portainer

* **Create** (UI and CLI): a form that sends a stack definition *to Portainer* - name,
  environment, Git repository + reference + compose path (using a Git credential Portainer
  already holds, chosen by name) or pasted compose text, and environment variables. The
  orchestrator keeps only the link and policies it creates for it. Variable values go straight
  through; they are not stored or logged here.
* **Delete**: removes the stack in Portainer, then the link. Asks for the stack name, refuses
  while the sub-service is on or has linked proxy hosts unless told to take them down first
  (the enable/disable cascade below does it in the right order), and writes an audit entry.
* Single containers are not created or deleted here - they belong to stacks.

## Image or build: both are supported

Some stacks pull a published image, many **build from source in the Git repository**. The
orchestrator reads which (from the compose file, as a label shown in the UI) and the deploy
adapts:

| | Redeploy does | Pre-deploy state worth keeping | Rollback possibilities |
|---|---|---|---|
| **image** | git pull, then pull the image tags, recreate | the image ids the containers use now | previous Git ref; or put the previous image back under its tag (research, see below) |
| **build** | git pull, then build, recreate | the image ids / the deployed Git hash | previous Git ref (rebuilds); the old image id may still exist untagged |
| **neither is pinned** (your case today) | - | - | a Git ref does *not* reproduce the old app for image stacks on a moving tag |

Because nothing is pinned, **rollback is not assumed to work**. The first version offers the
choices that are safe and honest, per application (`on_failure`):

* `notify` - say that it failed, leave it as it is (default);
* `stop` - take the stack down and notify. Fine for dev and test stacks: a stopped service
  with a notification is a legitimate outcome;
* `restore_backup` - propose restoring the guest backup taken before the deploy, through the
  existing approval page (stop guest, restore, start). Never automatic: it throws data away;
* `redeploy_previous` - best effort, later, once researched against your Portainer version
  (does git redeploy accept a commit as the reference? can the Docker proxy re-tag the old
  image so a no-pull redeploy brings it back?). Not in the first cut.

## Backups

* **Default: the whole guest** the stack runs on, through the existing wake + backup pipeline,
  with a freshness rule - "a verified backup newer than N hours, else take one" - so a deploy
  does not always mean a full VM backup.
* A sub-service may later declare its own `backup: {method: dump, ...}` - a command run in a
  container (through Portainer's exec) that produces a dump, plus where the dump goes. That is
  a research item (exec through Portainer, where dumps are stored, restore of a dump); until
  then `method: guest` is the only method and sub-services without their own simply inherit it.
* A restore is a **guest backup restore** (with approval), or - for dev and test - the
  application is allowed to stay down with a notification (`on_failure: stop`).

## Enable and disable: one switch for everything linked

A sub-service has *linked controllers*: its proxy host(s), its stack (or container), and the
guest it runs on. Their states are **read, not remembered**:

| Linked thing | Read from | "on" means |
|---|---|---|
| proxy host(s) | Traefik Manager (state sync already in place) | enabled and served |
| stack / container | Portainer | stack active, containers running |
| guest | Proxmox | guest running |

The sub-service shows **on** (all linked things on), **off** (all off) or **mixed** - with the
exact parts that disagree ("stack stopped, proxy still served"). The service page and the map
show it per linked thing.

* **Disable** applies *off* to every linked controller, **outermost first**: unserve the
  proxy hosts, stop the stack, then (for a service that owns a guest) stop the guest.
  **Enable** goes the other way, **innermost first**: wake and start the guest, start the stack,
  wait for probes, then serve the proxy hosts.
* **Disabling a parent does the same to every descendant** (their proxy hosts and stacks) before
  its own linked things; enabling a parent only brings up its own things and leaves a child that
  was off, off, unless asked ("enable everything").
* Anything that would hurt is refused as today: a guest backup running, a deploy in flight.
  Stopping a guest asks for confirmation each time.
* **The intent flag stays.** `enabled` in `services.yaml` records what you asked for, because
  auto-recovery must be able to tell "stopped on purpose" from "crashed", and a restart should
  not decide that on its own. It is only intent: the state shown is always the observed one.
* **Mixed is shown, never auto-fixed.** If someone stops the stack in Portainer, the
  sub-service shows mixed and offers two buttons - *apply on* / *apply off* (make everything
  agree with one or the other) - and records the outside change in Activity.

## Link-up: adopting a stack (UI wizard and CLI)

1. **Discover**: environments, stacks and their containers (redacted).
2. **Pick** a stack -> proposes a sub-service under the guest Portainer runs on, with children
   for its containers (the DB one pre-flagged).
3. **Link proxy hosts**: proposes existing proxy hosts whose upstream `ip:port` matches a
   published port of one of the stack's containers; or attach any by hand. From the service
   page as well as the proxy host form.
4. **Probes**: proposes an HTTP probe through the proxy host, a "container healthy" probe per
   container, and a TCP probe for the DB port.
5. **Deploy**: webhook / manual only, and guard presets (none, probes, probes + backup). A
   webhook gets a generated token, **shown once**.
6. **Review and save**, like every other change.

CLI: `orchestrator-cli portainer endpoints|stacks|containers|logs`,
`stack adopt|create|delete`, `service link-host|unlink-host <slug> <hostname>`.

## The stack page (debugging and managing from one place)

* **Overview**: the three linked states (proxy / stack / guest) and whether they agree, the
  containers (state, health, image, restarts), `image` or `build`, deployed Git hash (and "N
  commits behind" only if a Git token is configured), probes, last backup, last deploys.
* **Logs**: pick a container, tail N lines, follow, download (`stack logs -f`).
* **Deploys**: history with each step; **Redeploy now** guarded, or - with a confirmed, audited
  override - unguarded; **Run guards only**.
* **Control**: enable / disable, start / stop / restart a stack or one container.
* **Open in Portainer / Traefik Manager / Proxmox** links for each linked thing, plus the host
  commands that do the same by hand.

## Deploys

A deploy is a `PipelineRun` of a new kind `deploy` - so it appears in Activity, the filters,
notifications and push with no new plumbing.

```
trigger (webhook | UI | CLI | seen-from-outside)
  -> lock check            (off? deploy already running? guest backup running?)
  -> pre-guards            (probes healthy? recent verified backup, or take one)
  -> deploy                (Portainer redeploy; pull for image stacks, build for build stacks)
  -> post-guards           (stack running, probes healthy in time, then stay healthy)
  -> succeeded | failed -> notify, then on_failure
```

**Ways a deploy begins** (all through the same guards):

* **Webhook to the orchestrator** (GitLab / GitHub / generic token verified): guards are
  *enforced* before Portainer is asked to redeploy. One option among several.
* **Portainer's own webhook**: the orchestrator notices the stack's update time / Git hash
  changed, records a deploy "from outside" and runs the **post** checks anyway. Pre-guards
  cannot run after the fact.
* **UI / CLI**: Redeploy now.
* **Later: GitLab pipelines**, once we see what adding GitLab to the setup gives (pipeline
  stages that call the guards, deployments reported back to GitLab).

## CLI parity (everything above)

`portainer {endpoints,stacks,containers,logs}`,
`stack {adopt,create,delete,show,start,stop,restart,logs,redeploy,deploys}`,
`deploy {run,guards,history,show}`, `service {link-host,unlink-host,enable,disable,state}`,
and the connectivity check. Each prints what it did and the audit entry it wrote.

## Build order

1. **Portainer client + read side**: shared client, redaction, version check, connectivity
   check, discovery API and CLI, container state and a log viewer. Read-only, risk-free.
2. **State and the one switch**: observed state per linked thing (proxy / stack / guest), the
   mixed display, enable/disable across all linked controllers and down the tree, the stack
   page (overview, control, logs, links out). Also finishes the old "disable does something on
   Proxmox" item for root services.
3. **Adoption, create and delete**: the wizard (UI + CLI + API), create/delete a stack through
   Portainer, proxy-host linking from the service side, probe and child suggestions.
4. **Deploy method and runs**: `deploy:` config, `PipelineKind.deploy`, manual redeploy for image
   and build stacks, outside-deploy detection with post checks, history in Activity.
5. **Guards**: pre probes, the backup freshness rule, the post health gate, `on_failure`
   (notify / stop / restore-with-approval), the webhook with provider verification and its lock.
6. **Polish**: map and tree show stacks and deploy state, deploy filters and notification
   events, docs. Later research: dump backups, `redeploy_previous`, GitLab.

Each step ships on its own and is useful alone; 1-3 change nothing about how you deploy today.

## Redundancy (not built now; kept possible)

A second cluster with its own Portainer, the same application on both, a synced database and
one Traefik route over both. What we do now so it can come later without rework:

* a controller names its Portainer by **instance** (`instance: main`, the default), so a second
  Portainer is a second named instance, not a rewrite;
* a service can later belong to a **replica group** (the two deployments of one application),
  with deploys, guards and health judged per replica and for the group;
* nothing assumes one Portainer, one cluster or one Traefik upstream.

The editable adapter instances and the secret store discussed earlier are a separate track and
are not needed for steps 1-6 (the Portainer URL and key stay in `.env` for now).

## Still open

1. **Intent vs fully derived**: keep `enabled` as intent next to the observed state (as above,
   because recovery needs to know "off on purpose"), or derive everything from observation and
   treat a stopped stack as simply off?
2. **Enabling a parent**: bring back only its own things (children stay off), or everything
   under it as it was before the disable? (As written: only its own, plus an explicit
   "enable everything".)
3. **Portainer version and edition**: step 1's connectivity check will report it; the exact API
   shapes (commit references on redeploy, saved Git credentials on create) depend on it.
