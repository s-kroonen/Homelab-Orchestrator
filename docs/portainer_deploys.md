# Portainer stacks as sub-services: on/off, deploys and guards

One deployed application - its Portainer stack, its containers (the database included), its
proxy host(s) and its deploy pipeline - is **one sub-service** here, with one switch, one place
to look, and a deploy that can be guarded by probes and a backup. Everything below works from
the dashboard **and** from `orchestrator-cli`; the design and the decisions behind it are in
[plan_portainer_deploys.md](plan_portainer_deploys.md), reading Portainer is in
[portainer.md](portainer.md).

The running example is `dev.xymbols`: a stack with its own proxy host, a database container and
a Git-webhook deploy.

## Principles

* **Portainer owns stacks, the way Proxmox owns guests.** The orchestrator reads what exists,
  and creates or deletes only *through Portainer's API*. A sub-service keeps a link
  (`controller: {type: portainer, endpoint: 1, stack: dev-xymbols}`) and the policies Portainer
  cannot hold (guards, history). No compose file, Git URL, branch or environment is stored here.
* **Observed state is shown; the registry only records what you asked for.** On, off or mixed is
  read from Traefik Manager, Portainer and Proxmox every time. Outside changes are shown and
  recorded, never silently reverted.
* **Secrets stay in Portainer.** Stack environment values and Git passwords go straight through
  to Portainer and are never stored, logged, audited or shown here. The one secret kept here is
  the deploy webhook's own (see below).
* **Everything else about the stack can still be done in Portainer** - each object links there.

## Linking a stack: adopt, or create

**Adopt** (Portainer page -> a stack -> *Adopt as a sub-service*, or `orchestrator-cli stack
adopt 1 dev-xymbols`) reads the stack and proposes:

* **one** sub-service for the whole stack under the guest Portainer runs on (you pick it; one that
  is called "portainer" is preselected). Its containers are not services of their own: each gets
  a check on the stack's sub-service, `container:<name>` (a `portainer` probe). A database image
  (postgres, mariadb, redis, ...) is flagged *backup waits for it* (`backup_waits`), so a backup
  of the guest waits for that container. Stacks adopted before this had a child per container;
  they are folded into the stack once, at startup (`stack.folded` in Activity; see
  [service_tree.md](service_tree.md)): their checks, proxy hosts and a database dump move to the
  stack (the dump then names its container). A container with its own recovery or deploy
  settings, or a second dump, stays a sub-service;
* the proxy hosts whose upstream points at a port the stack publishes;
* probes: a **`portainer`** probe per stack/container (running, none unhealthy - no SSH needed)
  and an HTTP probe through the first proxy host;
* a deploy method: none, or a guard preset (below), and optionally a webhook.

Everything is created through the same code as the service API (every check applies) and is
undone if a later step fails. `orchestrator-cli stack suggest 1 dev-xymbols` shows the proposal
without creating anything.

**Create** (*New stack...* on the Portainer page, `stack create`) sends a stack definition to
Portainer - from a Git repository (optionally with a credential Portainer already holds) or from
compose text - and can adopt it in the same step. The **exact request is shown first with every
secret masked**; nothing is sent until you confirm.

**Delete** (*Delete...*, `stack delete`) asks you to type the stack's name, removes the stack in
Portainer (its **volumes are kept**) and then the link. It refuses while a linked service is
switched on unless you ask it to take them down first - in the right order. Proxy hosts are never
deleted, only unlinked.

**Start / stop / restart** a stack or one container: the stack page (Containers -> a stack),
`stack start|stop|restart ENV NAME`, `stack start-container|...`. The stack page also kills,
recreates and inspects containers and edits the stack's environment and Git settings
([portainer.md](portainer.md)).

### Proxy hosts

Linking is done from the service page (*On / off & deploys* tab) or `service link-host SLUG
HOSTNAME [--force]` / `unlink-host`. A host is moved from another service only with *force*. The
route in Traefik Manager changes on the next push, like every other proxy host edit.

## One switch for everything linked

A service's linked things: its **proxy hosts**, its **stack or container**, and (for a root
service) its **guest**. The service shows **on**, **off** or **mixed** - with the exact parts that
disagree ("stack off, proxy served").

| Thing | Read from | "on" means |
|---|---|---|
| proxy host | Traefik Manager | the route exists and is enabled |
| stack / container | Portainer | the stack is active / the container runs |
| guest | Proxmox | the guest runs |

A proxy host switched off on its own is left out of the comparison - that was its own decision.

* **Switch off** goes outermost first, and everything *below* the service goes before it: proxy
  hosts are unserved, stacks stop, and only when you tick *also stop the Proxmox guest* (asked
  every time) the guest stops. `service disable SLUG [--stop-guest]`.
* **Switch on** goes innermost first: the guest, then the stack, **waited for until every
  container runs and none is still starting**, then the proxy hosts are served. A service under a
  switched-off parent is refused ("switch the parent on first"). `service enable SLUG`.
* Sub-services below a service that comes back are **only watched** by default (they keep their
  own intent and show as *mixed* if their stack is still down): the server's own start-up and the
  other control planes already decide what starts when. Tick *also start the sub-services that
  were on* (`--restore-children`) to start them.
* **Mixed is shown, never auto-fixed.** Two buttons, *Apply on* and *Apply off*, make everything
  agree; they are the same switch (it skips what is already right).
* Refused: a guest backup running, a deploy or another switch in flight.
* A switch is a pipeline run (Activity -> *on/off*), and a failed one sends a `switch_failed`
  notification. The registry's `enabled` flag stays the *intent*; it is written first when
  switching off and last when switching on.
* **Outside changes are noticed.** Every health cycle, a service whose parts disagree with what
  was asked for - and keeps disagreeing for two cycles - is written to Activity
  (`service.outside_change`) and announced once (`outside_change`). It is cleared when it agrees
  again. Nothing is changed back.

`orchestrator-cli service state SLUG` prints the same table as the page.

### When the node or guest is off

Everything a stack or container runs on lives on the root service's guest, so the state takes
that into account (the same node + guest check the health gate uses):

* **Node offline, or guest stopped:** the stack, container and controller are shown **off** with the
  reason ("not running: node 'pve1' is offline"), Portainer is not even asked, a still-served proxy
  host says "nothing is running behind it", and the state is **off** with the note *asked to be on,
  but node 'pve1' is offline* - not "mixed" and not "unknown". The map chip reads "host is off".
* This is not reported as "changed outside the orchestrator": a node that is off is a health alert's
  business.
* **Switching on** under an offline node stops with a failed `host` step that says so (wake the
  service or the node first); a merely *stopped* guest on a live node is started by the switch as
  before. Switching off skips parts that are already off.
* **Proxmox itself unreadable** stays "unknown": the orchestrator does not guess off.

### What counts as "changed outside"

* A proxy host that was **never pushed** to Traefik Manager is *pending*, not changed: it shows
  "not pushed yet (Push now)", is left out of the agreement and does not raise an alert. Press
  **Push now** (or `orchestrator-cli proxy push`) to create it. A host you switched **off** is
  pushed too, but stays off (created parked, disabled last), and never counts either way.
* A route that was pushed and is **gone** from Traefik Manager is a real change.
* What was already reported is read back from the audit log after a restart or an update, so a
  standing disagreement is announced **once**, not after every restart; it is announced again
  only when the disagreement itself changes or after it has been cleared.

## Deploys

A service linked to a stack that was deployed from Git can have a **deploy method**
(`deploy:` in `services.yaml`; the *Deploys* section of the tab; `deploy set`):

```yaml
deploy:
  trigger: {webhook: {provider: gitlab, branch: dev}}   # or null: manual only
  guard:
    pre:  {probes: true, backup: {max_age_h: 24, take_if_older: true}}
    post: {probes: true, timeout_s: 180, settle_s: 30}
  on_failure: notify        # notify | stop | restore_backup
  pull_image: true
  prune: false
```

A deploy is a pipeline run of its own kind, so it shows in Activity (filter *deploys*), in
notifications and in push:

```
lock           switched off? another deploy/switch running? a guest backup running?
pre-guards     probes healthy?   a verified backup newer than N hours - or take one
snapshot       what is deployed now (Git commit, image ids) - recorded, never secret
redeploy       Portainer pulls the repository, pulls / builds, recreates
wait up        every container running and settled
post-guards    probes healthy within timeout_s, then healthy for settle_s
```

* A **pre-guard that says no changes nothing**: the run ends *skipped* and a `deploy_blocked`
  notification says why. Unknown health also blocks - it cannot be verified.
* The **backup guard** uses a *verified* backup of the guest the stack runs on; if the newest is
  older than `max_age_h` it is taken first (`take_if_older`) through the normal wake + backup
  pipeline, or the deploy is refused.
* After the redeploy, a failure ends the run *failed* (`deploy_failed`) and then does
  `on_failure`: **notify** (leave it as it is), **stop** (switch the service off - fine for dev
  and test; a stopped service with a notification is a legitimate outcome), or
  **restore_backup** (only *proposes* restoring the backup through the approval page; it is never
  automatic because it throws data away).
* **Nothing is assumed to roll back.** Compose files here pin nothing and most stacks build from
  source, so a Git ref does not reproduce the old app. `redeploy_previous` is not offered.
* **Image and build stacks both work**: the redeploy is Portainer's Git redeploy (with *pull
  images* on or off). A stack Portainer did not create from Git cannot be redeployed.

### Ways a deploy begins

* **UI / CLI**: *Redeploy now*, `deploy run SLUG`. *Run the guards only* / `deploy guards SLUG`
  checks the pre-guards without deploying (and without taking a backup). *Redeploy without
  guards...* / `deploy run --unguarded --reason "..."` skips the pre-guards; it needs a written
  reason and is audited (`deploy.unguarded`). The post-guards always run.
* **Webhook to the orchestrator** (below): guards are *enforced* before Portainer is asked.
* **Portainer's own webhook / auto-update / its UI**: noticed from the stack's Git commit
  changing (stopping and starting a stack does not change it), recorded as a deploy "from
  outside" and given the **post** checks and `on_failure` - pre-guards cannot run after the fact.
  The watcher compares from the first time it looks, so a deploy while the orchestrator was down
  is not noticed.

### The webhook

`POST /hooks/deploy/{slug}` (public, authenticated by the service's own secret):

| Provider | Authentication | Events |
|---|---|---|
| `generic` | `Authorization: Bearer <secret>`, `X-Deploy-Token`, or `?token=` | any; optional JSON `{"ref", "sha", "pusher"}` |
| `gitlab` | `X-Gitlab-Token: <secret>` | Push Hook (others are acknowledged and ignored) |
| `github` | `X-Hub-Signature-256` (HMAC of the exact body) | push (`ping` answered; others ignored) |

* **Branch filter**: with a branch set, a push to another branch is acknowledged and ignored.
* Answers: `202` started (or acknowledged), `401` bad secret, `404` no such webhook (also for a
  service without one), `423` the service is switched off, `409` busy or deploys disabled.
* The **secret** is created or replaced from the tab (*Create / replace the secret*) or `deploy
  webhook SLUG [--rotate|--remove]`, and **shown once**. It is kept in the database (`setting`
  table, so in the System page's backups) - never in `services.yaml` and never in an API response
  after it was created. Rotate it if it leaked. The tab and the CLI show what to enter in GitLab
  or GitHub.

### Calling it from GitHub Actions

Every service has **its own key** (a key opens its own webhook and its own run status, nothing
else). The *Webhook* section of the tab has a **GitHub pipeline** block - a complete workflow
(`.github/workflows/deploy-<slug>.yml`) to copy; `deploy webhook SLUG --github` prints the same.

* The key is read from a repository secret named after the service (`DEPLOY_KEY_DEV_XYMBOLS`):
  add it once under *Settings > Secrets and variables > Actions*. The block never contains the key
  unless you tick *write the key into the block* - possible only right after the key was created,
  and not something to commit.
* Any provider's webhook also accepts the key as a bearer token, which is what the block uses.
* The job **fails with the reason** when: the key is missing; the orchestrator cannot be reached
  (down, wrong URL, not reachable from the runner - a GitHub-hosted runner needs a public address,
  otherwise use a self-hosted runner); the key is refused (401); the webhook is not switched on
  (404); the deploy is refused (409 busy / deploys disabled; 423 service switched off); the
  orchestrator itself errors (5xx).
* It then **waits for the result** (`GET /hooks/deploy/{slug}/runs/{id}`, same key, only step
  names and reasons): *skipped* means a guard blocked it before anything changed, *failed* shows
  the failing steps (for example the probe that never turned healthy), and the job only succeeds
  when the deploy ended healthy. A push to another branch than the one set is acknowledged and
  the job ends successfully without deploying. It gives up after 30 minutes and says the deploy
  may still be running.
* Without `ORCHESTRATOR_PUBLIC_URL` the block has a `YOUR-ORCHESTRATOR` placeholder URL.

## Reference

| | UI | CLI | API |
|---|---|---|---|
| state | On / off tab | `service state SLUG` | `GET /api/services/{slug}/linked`, `/linked/all` |
| switch | On / off tab | `service enable|disable SLUG` | `POST /api/services/{slug}/switch` |
| link hosts | tab | `service link-host|unlink-host` | `POST|DELETE /api/services/{slug}/proxy-hosts`, `GET .../proxy-host-candidates` |
| adopt | Portainer page | `stack suggest|adopt` | `GET /api/stacks/suggest`, `POST /api/stacks/adopt` |
| create / delete | Portainer page | `stack create|delete` | `POST /api/stacks/preview`, `POST /api/stacks`, `DELETE /api/stacks/{env}/{name}` |
| stack / container control | Portainer page | `stack start|stop|restart[-container]` | `POST /api/stacks/{env}/{name}/{action}`, `/api/containers/...` |
| deploy settings | tab | `deploy set|show|clear` | `PUT /api/registry/services/{slug}` (`deploy`) |
| run / history | tab | `deploy run|guards|history|show` | `POST /api/services/{slug}/deploy`, `GET .../deploys`, `GET /api/deploys/{id}` |
| webhook | tab | `deploy webhook` | `POST|DELETE /api/services/{slug}/deploy/webhook`, `GET .../deploy` |

New notification events: `switch_failed`, `outside_change`, `deploy_blocked`, `deploy_failed`
(sent by default) and `deploy_succeeded`, `deploy_outside` (in the feed unless a rule asks).

## Settings

* `PORTAINER_READ_ONLY=true` makes every write refuse ("Portainer is read-only here"): the
  Portainer page, the switch, deploys and the legacy start/stop controller all honour it. Use it
  while you are checking the read side against your Portainer.

## What is not verified against a real Portainer yet

The write calls were built from Portainer's documented API and tested against fakes, not against
your Portainer: the **stack start / stop / Git redeploy / create / delete** request shapes, a
redeploy keeping the stack's environment (the current one is read and sent back, never exposed),
saved Git credentials on create, and the "Open in Portainer" link shapes. The Connectivity check
reports the version and edition. Create shows the request before sending it, and
`PORTAINER_READ_ONLY` exists for exactly this: try the read side first, then switch writes on and
start with `stack stop` / `start` of a throwaway stack.

## Not built (kept possible)

* ~~Backups of a sub-service on its own~~ - built as **dumps**, see [dumps.md](dumps.md).
* `redeploy_previous`, GitLab pipelines reporting back, a second Portainer / redundant replicas
  (controllers can name an `instance` later without a rewrite), and rules for what a parent
  coming back should start.
