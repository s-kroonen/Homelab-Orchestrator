# Design rules and wireframes

The dashboard is being rebuilt to one consistent design (the plan and the measurements behind it
are in [plan_ui_overhaul.md](../plan_ui_overhaul.md)). The wireframes below are the **approved
target**: build every page to match them, and follow the rules on this page for anything they do
not show.

## How we work

1. **A page that has a wireframe is built to it.** Layout, navigation, which actions exist and
   where they sit, how editing works. Small deviations (a field the wireframe did not know about)
   are fine; a different pattern is not.
2. **A new page, or a big change to one, gets a wireframe first.** Draw it on the design canvas,
   following the rules below, get it approved, then add it here (source, HTML and PNG, see
   [Updating the wireframes](#updating-the-wireframes)) in the same pull request as the code, or
   before it. Small changes inside an existing pattern (a new column, a new field in a drawer)
   need no wireframe.
3. **Wireframes in this repo use example data.** The repository is public: no real hostnames,
   addresses, domains or names. Use `*.example.lan`, `10.0.x.x`, `node-a/b/c`.
4. **When the code and a wireframe disagree,** fix the code, or update the wireframe in the same
   pull request and say why.

## The five rules

1. **Navigate by what you manage.** Every object (service, node, proxy host, backup, connection)
   has one home with a list and a detail view. Other pages link to it instead of repeating it.
2. **One way to edit.** Things are shown read-only. *Edit* (or *+ New*) opens the same side drawer
   everywhere: fields, inline errors under the field, *Cancel* and *Save* at the bottom. No inline
   row editing, no separate form pages, no `prompt()`.
3. **One save model.** *Save* applies the change. Writing `services.yaml` and pushing to Traefik
   Manager happen after it by themselves. Anything still pending (a push that failed) shows in
   **one** place: a pill in the header with *Retry*. No *Save to YAML*, *Reset* or *Push* buttons
   on pages.
4. **Update in place.** A page is made of sections that refresh themselves on live events. Never
   `location.reload()`. Each live section says how old its data is ("updated 8 s ago").
5. **Fast first paint.** Show the last known state immediately, live data swaps in behind it.
   Show a skeleton, not "Loading…", for the rare section that has nothing yet.

## Navigation

```
Overview   Dashboard · Map · Inventory
Services   Services (list) → service detail · Backups
Sources +  All sources
           ▾ Proxmox    ● cluster-a  ● cluster-b
           ▸ Portainer 2  ●          (folded; a dot when something in it is wrong)
           ● Proxy · gateway          (one instance: one line)
           ● Backups · pbs-main
           ● Power · mqtt
           ▾ Machines   ● nas-01  ● backup-pi
Activity   Activity · Traffic
Settings   General · Notifications · AI assistants · Admins & devices · System · Archive
```

**Sources first.** Every connection (a Proxmox cluster, a Portainer, a proxy, a PBS, a power
manager, a bare-metal machine, later Pterodactyl, Nextcloud or TrueNAS) is a *source* with its own
page. A source page is the one home of what that source has: its nodes and guests, stacks and
containers, proxy hosts. Create, edit, delete and bulk actions happen there, for that source only.

* **Inventory** is the secondary view across sources: one list per kind of thing, linked to a
  service or not, every row linked to its source page.
* **Global search** finds anything in any source, plus services, settings and pages.
* **Links between sources are always shown and clickable**: the VM a Portainer environment runs
  on, the proxy host pointing at a guest, the machine hosting an environment.
* **Linking works from both sides**: *Link to a service…* or *Create a service from this…* on a
  stack, guest, container or proxy host; *Where it runs* on a service picks them per source.
* **Notifications open the nearest page**: the item under its source (a failed deploy opens
  Sources / Portainer / home / wiki), never a page across sources.
* Old URLs do not redirect.
* Actions across several sources get their own Bulk actions page (later).

Being built: until each step lands, the pages it replaces (Nodes & guests, Proxy
hosts) still exist; each is removed, with its wireframe, in the step that
replaces it.

Header on every page: breadcrumb, global search (`/`), the pending pill when something is
pending, the live indicator, the user menu.

## Page layouts

| Layout | Used for | Shape |
|---|---|---|
| **List** | Services, Backups, Inventory, every resource tab of a source page | Title with count and one primary action · filter box + filter chips · table with sortable columns · `⋯` row menu for the rest · bulk bar when rows are selected · footer with count and age |
| **Source** | Every source | Name, kind, status, address and "used by" in the header with *Test* and `⋯` · tabs *Overview* · one tab per kind of resource · *Settings* · *Raw* (tabs in the URL) · the resource tabs are List layouts |
| **Detail** | A service, a stack, a guest, a machine | Title, status and the primary actions in the header, the rest in `⋯` · tabs *Overview · Configuration · Related · History* (tabs in the URL) · facts column on wide screens |
| **Settings** | A source's Settings tab, General, Notifications, System… | Short cards, each saved on its own; help folded behind a link |
| **Drawer** | Every create and edit | From the right, 480–520 px, dims the page; title says what is edited; *Test* where it can be tested; footer says what Save will do |
| **Confirm** | Every destructive action | Centred dialog that names what else is affected; destructive ones (power off, delete, restore) ask to type the name |
| **Selection sidebar** | Map | Full-height panel on the right with the selected item's state, why, actions, details, path, backups, recent |

## Look

| Token | Value | Use |
|---|---|---|
| background | `#0d1015` | page |
| sidebar | `#11151c` | navigation |
| surface | `#141922`, border `#232a36` | cards, tables, drawers |
| inset | `#0f131a` | fields, chips, tiles inside a card |
| text / muted / faint | `#e6e9ef` / `#9aa4b2` / `#8a94a5` | body / secondary / labels |
| accent | `#4f7fe0` (links `#8fb3ff`) | primary button, selection, current tab |
| radius | 8 px buttons and fields · 10–12 px cards · pill 999 px | |
| type | Inter 14 px body, 24 px page title, 15 px section title, 12 px meta, 11 px uppercase lane labels; JetBrains Mono for slugs, hosts, IPs | |

**Status vocabulary: one word, one colour, everywhere:**

| Class | Means | Colours |
|---|---|---|
| `ok` | healthy, on, reachable, in sync | `#11331f` / `#5fe08b` |
| `warn` | needs a look (changed outside, backup failed, nearly full) | `#3a2c10` / `#f0b44c` |
| `fail` | failed, unreachable, error | `#3d1717` / `#ff8a82` |
| `off` | off, asleep, stopped, not set up, unknown | `#232834` / `#b3bccb` |
| `pending` | not pushed yet, running, queued | `#162a45` / `#8fb3ff` |

Something that is off on purpose (a node asleep, its services) is **off**, never **fail**; say why
("asleep: its node is off").

## Components

* **Buttons:** one primary (filled blue) per view. Secondary are grey. Destructive actions sit in
  the `⋯` menu (red text) or in the confirm dialog, never as a red button in a row. Icon-only
  buttons carry an `aria-label`.
* **Feedback:** results of an action → toast; a wrong field → message under the field; a state
  that persists (pending push, database upgrade, dry run) → banner or header pill. No per-section
  message lines.
* **Tables:** sortable headers (▲▼), wide tables scroll inside their card, never the page.
* **Empty and off states:** say what to do next ("No services on this node yet. Import its
  guests").
* **Help text:** one sentence under the title; anything longer behind a "How this works" link.
* **Phone (under 40 rem):** bottom tab bar (Dashboard · Services · Activity · More), status and
  actions first, 44 px touch targets, tables become cards, no horizontal page scroll.
* **Shared code:** the helpers, drawer, confirm, tabs, table and live sections come from one
  shared script (`static/ui.js`) and one stylesheet (`static/app.css`); the drawer and actions
  of each object (service, node, proxy host) from `static/objects.js`, so every page edits it
  the same way; the header from `static/shell.js`. Pages do not define their own copies (`$`, `el`, `setMsg`…),
  their own `<style>` blocks or colours outside the tokens above.

## The wireframes

| Screen | Wireframe |
|---|---|
| Dashboard: what needs attention, nodes, recent activity | [PNG](wireframes/Main.png) · [HTML](wireframes/Main.html) |
| Services list: filters, bulk bar, row menu (its *Sub-services* tab is the same table, one row per sub-service) | [PNG](wireframes/Services.png) · [HTML](wireframes/Services.html) |
| Nodes & guests *(replaced by Sources)* | [PNG](wireframes/Nodes.png) · [HTML](wireframes/Nodes.html) |
| Service: Overview tab | [PNG](wireframes/ServiceOverview.png) · [HTML](wireframes/ServiceOverview.html) |
| Service: Configuration tab, editing in the drawer | [PNG](wireframes/ServiceConfig.png) · [HTML](wireframes/ServiceConfig.html) |
| Service: Related tab (on/off chain, proxy, stack, backups) | [PNG](wireframes/ServiceRelated.png) · [HTML](wireframes/ServiceRelated.html) |
| Proxy hosts: list, tabs for import and format *(replaced by Sources)* | [PNG](wireframes/ProxyHosts.png) · [HTML](wireframes/ProxyHosts.html) |
| Proxy host: create / edit drawer | [PNG](wireframes/ProxyEdit.png) · [HTML](wireframes/ProxyEdit.html) |
| Confirm a destructive action | [PNG](wireframes/ConfirmPowerOff.png) · [HTML](wireframes/ConfirmPowerOff.html) |
| Backups: policies, snapshots, dumps, orchestrator backups | [PNG](wireframes/Backups.png) · [HTML](wireframes/Backups.html) |
| Map: one row per node, shared backups, selection sidebar | [PNG](wireframes/Map.png) · [HTML](wireframes/Map.html) |
| Traffic: one page, countries included | [PNG](wireframes/Traffic.png) · [HTML](wireframes/Traffic.html) |
| Activity: one feed, run details in the drawer | [PNG](wireframes/Activity.png) · [HTML](wireframes/Activity.html) |
| Stack: containers, details and logs | [PNG](wireframes/Stack.png) · [HTML](wireframes/Stack.html) |
| Stack: environment (edit, redeploy) and settings | [PNG](wireframes/StackEnv.png) · [HTML](wireframes/StackEnv.html) |
| Service: Sub-services tab (parent) | [PNG](wireframes/ServiceSubs.png) · [HTML](wireframes/ServiceSubs.html) |
| Sub-services in lists (Services, Dashboard, Map) | [PNG](wireframes/SubRollup.png) · [HTML](wireframes/SubRollup.html) |
| Settings: Notifications (rules, channels, this device) | [PNG](wireframes/Notifications.png) · [HTML](wireframes/Notifications.html) |
| Settings: AI assistants (waiting, connections, calls) | [PNG](wireframes/Assistants.png) · [HTML](wireframes/Assistants.html) |
| Settings: System (update, time, this orchestrator) | [PNG](wireframes/System.png) · [HTML](wireframes/System.html) |
| Settings: Archive | [PNG](wireframes/Archive.png) · [HTML](wireframes/Archive.html) |
| Settings: Admins & devices (passkeys, admins, invites) | [PNG](wireframes/Admins.png) · [HTML](wireframes/Admins.html) |
| Phone: dashboard | [PNG](wireframes/PhoneDashboard.png) · [HTML](wireframes/PhoneDashboard.html) |
| Phone: service | [PNG](wireframes/PhoneService.png) · [HTML](wireframes/PhoneService.html) |
| Sources: every source, its status and what it has | [PNG](wireframes/Sources.png) · [HTML](wireframes/Sources.html) |
| Sources: add a source | [PNG](wireframes/SourceAdd.png) · [HTML](wireframes/SourceAdd.html) |
| Source page: Proxmox overview | [PNG](wireframes/SourceProxmox.png) · [HTML](wireframes/SourceProxmox.html) |
| Source page: Proxmox guests (bulk bar, links to other sources) | [PNG](wireframes/SourceGuests.png) · [HTML](wireframes/SourceGuests.html) |
| Source page: Portainer containers by environment | [PNG](wireframes/SourcePortainer.png) · [HTML](wireframes/SourcePortainer.html) |
| A stack under its source (where a deploy alert lands): its place and header; the tabs are the Stack wireframes above | [PNG](wireframes/SourceStack.png) · [HTML](wireframes/SourceStack.html) |
| Source page: a proxy's proxy hosts (create, edit, link) | [PNG](wireframes/SourceProxy.png) · [HTML](wireframes/SourceProxy.html) |
| Source page: a bare-metal machine over SSH | [PNG](wireframes/SourceMachine.png) · [HTML](wireframes/SourceMachine.html) |
| Source page: Settings tab | [PNG](wireframes/SourceSettings.png) · [HTML](wireframes/SourceSettings.html) |
| Source page: Raw data | [PNG](wireframes/SourceRaw.png) · [HTML](wireframes/SourceRaw.html) |
| Link a stack, guest or proxy host to a service | [PNG](wireframes/SourceLink.png) · [HTML](wireframes/SourceLink.html) |
| Service: where it runs (guest, stack, proxy hosts per source) | [PNG](wireframes/ServiceWhere.png) · [HTML](wireframes/ServiceWhere.html) |
| Inventory: everything across sources, linked or not | [PNG](wireframes/Inventory.png) · [HTML](wireframes/Inventory.html) |
| Global search | [PNG](wireframes/Search.png) · [HTML](wireframes/Search.html) |

## Updating the wireframes

`wireframes/source/` holds the editable originals in the design canvas format (`canvas.json` plus
one `.dc.html` per screen). Change them on the canvas or in the files, then regenerate the HTML and
PNG and commit all three together:

```
node docs/design/render_wireframes.js docs/design/wireframes/source docs/design/wireframes
```

(Needs Playwright with Chromium.) A new screen is a new `.dc.html` plus its entry in
`canvas.json`, and a row in the table above.
