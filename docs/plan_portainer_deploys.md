# Plan: managing Portainer stacks and containers, deploys and guards

Goal: everything about one deployed application - its Portainer stack, its containers
(including the database), its proxy host(s) and its deploy pipeline - becomes **one
sub-service** in the orchestrator, with one enable/disable switch, one place to monitor it,
one place to debug it, and a deploy that can be guarded by backups and probes. All of it
from the UI **and** the CLI.

The running example is `dev.xymbols`: a Portainer stack with its own proxy host, database
and Git-webhook deploy.

## Principles (these decide most of the choices below)

1. **The orchestrator gathers and displays; it is never the source of truth.** Stack
   definitions, compose files, Git history and stack environment variables stay in
   Portainer and Git. The orchestrator stores only: the *links* (which stack, which proxy
   hosts), the *policies* (guards, disable behaviour) - both in `services.yaml`, like
   everything else it is configured with - and *run history* (a deploy happened, how it went).
   It reads live state from Portainer and never copies a stack's definition or its env.
2. **Every adapter has other control points.** Anything the orchestrator can do, Portainer
   (or the host) can still do directly. The UI links out to the object in Portainer and shows
   the equivalent host command; changes made outside are observed and recorded, never fought.
3. **Guards are optional, per application.** A simple pipeline needs none and stays a
   pass-through; a guarded one can refuse or abort a deploy.
4. **Secrets stay where they are** (Portainer stack env). The orchestrator must never
   display, log or store a stack's environment values; Portainer's API returns them, so every
   read of a stack is *redacted at the client*. (Moving them is a later, separate migration.)
5. **Designed for a second Portainer, not built for it** (see "Redundancy" at the end).

## What exists today

* Sub-services (`parent`), each with its own `enabled`, probes, `controller:`, `recovery:`,
  `backup_gate:`. Disabling a service already **parks its subtree**: its proxy hosts stop
  being served (host cascade) and the scanner skips it.
* A Portainer **controller**: start / stop / restart of one container or one stack through
  `PORTAINER_URL` + `PORTAINER_API_KEY`.
* Proxy hosts linked to a service (`service_id`, editable on the proxy host form).
* Pipelines with run history (`PipelineRun`) shown in Activity, notifications, push.

Missing: discovering what is in Portainer, richer state (containers, health, Git ref,
last update), logs, redeploy, a deploy method, guards, "disable also stops the stack", the
guided link-up, and the CLI for all of it.

## The model

```yaml
services:
  - slug: portainer-vm            # the guest Portainer runs on (exists)
    ...
  - slug: dev-xymbols             # NEW: a sub-service for the stack
    parent: portainer-vm
    controller: {type: portainer, endpoint: 2, stack: dev-xymbols}   # exists
    on_disable: stop              # NEW: disabling stops the stack ('none' = only unserve)
    deploy:                       # NEW: how it is deployed, and the guards around that
      method: portainer_stack     # portainer_stack | portainer_webhook | command
      trigger:                    # how a deploy starts without a human (optional)
        webhook: {provider: gitlab, branch: main}   # token generated, shown once
      guard:                      # all optional; none = pass-through
        pre:  {probes: true, backup: {max_age_h: 24, take_if_older: true}}
        post: {probes: true, timeout_s: 180, settle_s: 30}
        on_failure: notify        # notify | rollback
      rollback: {method: redeploy_previous}      # or restore_backup (needs approval)
    proxy_hosts: [{hostname: dev.xymbols.com, ...}]   # exists
  - slug: dev-xymbols-db          # optional child: the stack's database container
    parent: dev-xymbols
    controller: {type: portainer, endpoint: 2, container: dev-xymbols-db-1}
    backup_gate: required         # exists: a backup of the guest waits for it
```

* **Disable** a stack sub-service = (1) its proxy hosts are unserved (exists), (2) the stack is
  **stopped** (new, `on_disable: stop`, default on for stack controllers, always confirmed in
  the UI/CLI), (3) its deploy method is blocked - the guarded webhook answers `423 Locked`,
  manual redeploys are refused, (4) its probes pause (exists). **Enable** reverses it: start
  the stack, wait for probes, serve the hosts. Refused while a deploy is running.
  Portainer's own webhook still works if something calls it directly - the orchestrator
  cannot lock that - and a deploy that happens anyway is *observed* (below).
* Children (the DB) are normal sub-services: own probes, own recovery, own `backup_gate`.

## Reading Portainer (new `PortainerClient`, used by everything)

One shared client (today each controller builds its own httpx call), tolerant of
Portainer-version differences (it reads `/api/system/version` first and logs it), with
short in-memory caching and **redaction of env values at the boundary**.

| Need | Portainer API (to be confirmed against your version) |
|---|---|
| environments | `GET /api/endpoints` |
| stacks (name, type, status, Git URL/ref/hash, auto-update webhook id, updated at) | `GET /api/stacks`, `GET /api/stacks/{id}` |
| containers of a stack, state, health, image | Docker proxy: `GET /api/endpoints/{id}/docker/containers/json` + labels `com.docker.compose.project` |
| one container (health detail, restart count, published ports) | `.../containers/{id}/json` |
| logs | `.../containers/{id}/logs?stdout=1&stderr=1&tail=N&timestamps=1` |
| stats | `.../containers/{id}/stats?stream=false` |
| start / stop a stack | `POST /api/stacks/{id}/start`, `/stop?endpointId=` |
| redeploy from Git | `PUT /api/stacks/{id}/git/redeploy?endpointId=` (pull image, prune options) |
| Portainer's own deploy webhook | `POST /api/stacks/webhooks/{webhookId}` |

Added to the connectivity checks (`/infra`, CLI): "Portainer: reachable, API key accepted,
version X, N environments".

## Link-up: adopting a stack (UI wizard and CLI)

1. **Discover**: list environments, stacks and their containers (redacted).
2. **Pick** a stack -> proposes a sub-service under the guest Portainer runs on:
   slug from the stack name, `controller` filled in, child sub-services for its containers
   (the DB one pre-flagged).
3. **Link proxy hosts**: proposes existing proxy hosts whose upstream `ip:port` matches a
   published port of one of the stack's containers (on the guest's address); or attach any
   host by hand. (From the service page too, not only from the proxy host form.)
4. **Probes**: proposes an HTTP probe through the proxy host, a "container healthy" probe per
   container, and a TCP probe for the DB port.
5. **Deploy method**: webhook / manual redeploy / none, and guard presets (none, probes, probes +
   backup). A webhook gets a generated token, **shown once**.
6. **Review and save** like every other change (Save writes `services.yaml`).

CLI, same steps: `orchestrator-cli portainer endpoints|stacks|containers|logs`,
`orchestrator-cli stack adopt --endpoint 2 --stack dev-xymbols --parent portainer-vm
--link-host dev.xymbols.com --deploy webhook --guard probes+backup`,
`orchestrator-cli service link-host|unlink-host <slug> <hostname>`.

## The stack page (debugging and managing from one place)

* **Overview**: stack status, containers (state, health, image, restarts), deployed Git ref vs
  the branch head ("2 commits behind" - read from Git only if a token is given, otherwise just
  the deployed hash), proxy hosts and whether they are served, probes, last backup, last deploys.
* **Logs**: pick a container, tail N lines, follow, download. `orchestrator-cli stack logs -f`.
* **Deploys**: history with each step and its outcome, **Redeploy now** (guarded or, with a
  confirmed override that is audited, unguarded), **Run guards only**.
* **Control**: start / stop / restart stack or one container, enable / disable.
* **Open in Portainer**: links to the stack and to each container, plus the host commands that do
  the same by hand (shown, copyable).

## Deploys

A deploy is a `PipelineRun` of a new kind `deploy` - so it appears in Activity, the filters,
notifications and push with no new plumbing.

```
trigger (webhook | UI | CLI | seen-from-outside)
  -> lock check            (disabled? already deploying? backup running on the guest?)
  -> pre-guards            (probes healthy?  recent verified backup, or take one)
  -> deploy                (Portainer git redeploy, pull image)
  -> post-guards           (stack running, probes healthy within timeout, then stay healthy)
  -> succeeded | failed -> notify, and rollback if the policy says so
```

**Two ways a deploy begins:**

* **Guarded**: point the Git webhook at the orchestrator (GitLab / GitHub signature or token
  verified). It runs the guards, then asks Portainer to redeploy. Guards are *enforced*.
* **Observed**: leave the webhook pointing at Portainer. The orchestrator notices the stack's
  update time / Git hash changed, records it as a deploy "from outside", and runs the
  **post** checks anyway (so a bad deploy is still caught and announced). Pre-guards cannot run
  - they would have to be before the fact.

Both can coexist per application; the page says which one is in force.

**Backup before a deploy** reuses what exists: the wake + backup pipeline on the guest the stack
runs on, with a freshness rule - "a verified backup newer than N hours, else take one" - so a deploy
does not always mean a full VM backup. A DB-level dump (exec in the DB container through
Portainer) is a possible later backup method; it needs somewhere to put the dump, so it is a
question for you below, not part of the first cut.

**Rollback** is a choice per application, and both need care:
* `redeploy_previous` - redeploy the previously deployed Git ref (works if the compose file pins
  image versions, and if Portainer redeploy accepts that ref - to be verified);
* `restore_backup` - never automatic: it creates the same approval page the restore flow already
  has ("stop guest, restore, start") because it throws away data.

## CLI parity (everything above)

`orchestrator-cli portainer {endpoints,stacks,containers,logs}`,
`stack {adopt,show,start,stop,restart,logs,redeploy,deploys}`,
`deploy {run,guards,history,show}`, `service {link-host,unlink-host,enable,disable}`,
`connectivity check portainer`. Each prints what it did and the audit entry it wrote.

## Build order

1. **Portainer client + read side**: shared client, redaction, version check, connectivity
   check, discovery endpoints and CLI (`portainer ...`), container list/state/logs with a
   log viewer. Useful on its own, risk-free (read-only).
2. **Control and the disable rule**: stack and container actions through the client,
   `on_disable` / enable, the stack page (overview, control, logs, Open in Portainer).
3. **Adoption**: wizard (UI + CLI + API), proxy-host linking from the service side, probe and
   child suggestions, controller state as a probe kind.
4. **Deploy method and runs**: `deploy:` config, `PipelineKind.deploy`, manual redeploy,
   observed deploys (change detection) with post checks, deploy history in Activity.
5. **Guards**: pre probes, backup freshness rule, post health gate, rollback options, approvals,
   the guarded webhook with GitLab/GitHub verification and its lock behaviour.
6. **Polish**: map and tree show stacks and their deploy state, deploy filters/notification
   events, docs.

Each step ships on its own and is useful alone; 1-3 need no change to how you deploy today.

## Redundancy (not built now; kept possible)

A second cluster with its own Portainer, the same service deployed to both, a synced database and
a Traefik route using both. What we do now so it can come later without rework:

* a controller names its Portainer by **instance** (`instance: main`, default `main`), so a
  second Portainer is a second named instance, not a rewrite;
* a service can later belong to a **replica group** (the two deployments of one application),
  with deploys, guards and health evaluated per replica and for the group;
* nothing assumes one Portainer, one cluster or one Traefik upstream.

The editable adapter instances and the secret store discussed earlier are a separate track and
are not needed for steps 1-6 (the Portainer URL and key stay in `.env` for now).

## Open questions

1. **Guarded webhook**: are you willing to repoint the Git webhook for guarded applications at the
   orchestrator (keeping Portainer's own webhook as a bypass we only observe)?
2. **Backups for dev.xymbols**: the data lives in the stack's DB volume, inside the guest Portainer
   runs on - is the guest backup (PBS) the thing to rely on, with the freshness rule, or do you also
   want a DB dump? Where would a dump go?
3. **Rollback**: does the compose file pin image versions (so redeploying the previous Git commit
   gives you the previous app)? If not, rollback is "restore backup with approval" only.
4. **Disable stops the stack**: default on (confirmed each time), or opt-in per application?
5. **Portainer version and edition**: needed for exact API behaviour; step 1's connectivity check
   will report it, so this can wait.
