# UI overhaul: analysis and plan

**Status: analysis.** Nothing here is built yet. The functions are all there; what is missing is
one way of doing each thing. This document measures where the dashboard is today, names what
makes it feel like chaos, and proposes a target design and an order to build it in.

How it was measured: the current `dev` (PR #70) served in dry-run with
`config/services.example.yaml`, every page opened in Chromium (1280x900 and a 390px phone),
plus a read of every template and script. Dry-run contacts nothing outside, so the load times
below are the *best case*; on the Pi the calls that reach Proxmox, PBS, Portainer, Traefik
Manager or MQTT add seconds (see 1.2).

---

## 1. Findings

### 1.1 Size of the UI layer

* 33 templates and 23 scripts, about 12,000 lines. Half of it is in five places:
  `service_detail.html` (1,969 lines), `proxy_host_form.html` (800), `proxy_hosts.html` (641),
  `dashboard.html` (565), `traffic.js` (474).
* 26 of 33 templates carry an inline `<script>`; 17 carry their own `<style>`. Most page logic
  lives inside the HTML, so it is re-sent with every page view and never cached. The service
  page is **~100 KB of HTML** per view.

### 1.2 Page loading

| Page | Requests | KB | API calls on load | Calls that reach another system |
|---|---:|---:|---:|---|
| Dashboard | 14 | 103 | 5 | `/api/infra/nodes/status` (Proxmox + MQTT + every node's own API) |
| Service detail | 17 | 35 (+100 HTML) | 9 | `/linked` (Portainer, Traefik Manager, Proxmox), `/controller`, `/backups` (PBS), `/proxy-hosts` |
| Proxy hosts | 11 | 5 | 3 | `/api/proxy-hosts` (3 Traefik Manager calls: routes, runtime routers, config files) |
| Portainer | 11 | 23 | 5 | status first, then environments and stacks |
| Data sources | 9 | 20 | 3 | Proxmox, Traefik Manager, Portainer |
| Connectivity | 7 | 1 | 2 | `/api/infra/overview` (checks every connection) |
| System | 12 | 11 | 7 | update status (GitHub) |
| Every page | | | +1 | `/api/display` (timezone/format), the same answer on every page |

What makes it slow in practice:

1. **No shared cache of live reads.** Only Portainer has one (5 s). Opening Proxy hosts asks
   Traefik Manager three times; opening it again asks again. The background scanners already
   poll the same systems, but pages do not use what they found.
2. **Waterfalls.** The service page fires 9 calls; several wait on the slowest system. The node
   status call asks every node at once but answers only when the slowest has (up to 10 s for an
   unreachable node), so one node that is off holds back the whole nodes table.
3. **Uncached static files.** `/static/*` is served `Cache-Control: no-cache` with no version in
   the URL, so every page view revalidates 5 to 10 files.
4. **No loading skeletons.** 39 "Loading…" strings. Pages jump as sections arrive.

### 1.3 Background refresh

The live-update stream (`/api/events`, `live.js`) exists and works, but the two busiest pages
use it to call **`location.reload()`**:

* Dashboard: a full reload on any `run`, `health` or `registry` event (debounced 3 s), plus a
  60 s fallback timer.
* Service page: the same for events about that service, plus 60 s.
* 33 places reload or navigate after an action instead of updating what changed.

Every reload re-downloads ~100 KB on the service page, re-runs all 9 API calls (re-asking
Portainer, Traefik Manager and PBS), resets scroll, and closes anything not guarded. The code
needs a `shouldSkipAutoReload()` with six special cases (typing, open probe editor, open stack
panel, selected rows, backup in flight...) to avoid destroying what you are doing. Other pages
have their own timers: Portainer logs every 3 s, approvals every 4 s, time controls every 1 s.
There is no "updated 12 s ago" anywhere.

### 1.4 Navigation and information architecture

The sidebar has 16 entries in 5 groups, grouped by *which system the data comes from* rather
than *what you are doing*:

* **Monitoring** holds Activity, Portainer, Notifications, AI assistants, Connectivity and
  Snapshots: four of those are configuration or infrastructure, not monitoring.
* **Backups** holds only Backup policies; PBS snapshots are under Monitoring; self-backups are
  under Orchestrator → "System and backups"; dumps live on each service page.
* **Services have no list page.** The service list is the top of the dashboard, together with a
  "New service" form, the nodes table, *My devices* and *Invite another admin*.
* The same object appears in many places, each with different actions:

| Object | Shown / acted on in |
|---|---|
| Nodes | Dashboard (edit, power, delete), Data sources → Proxmox (pull, import), Connectivity → Proxmox page (health), Map |
| Guests | Connectivity → Guests (start/stop), Data sources → Proxmox (import), service page Actions |
| Proxy hosts | Proxy hosts (+ separate form page), Data sources → Traefik Manager, Format routes, service page (two separate "Proxy hosts" sections: one tab, one in On/off) |
| Stacks | Portainer page, Data sources → Portainer (adopt), service page stack panel, Connectivity → Portainer page |
| Backups | Backup policies, Snapshots, System and backups, service page Backups tab, Dumps panel |

* Tabs exist in three different implementations: link tabs (Connectivity), JS tabs with a hash
  (service page), JS tabs without (Data sources).
* The service page puts its identity ("homelab-orchestrator / Example Media") into the sidebar,
  and Save to YAML / Reset to saved above its tabs, where every other page puts its title.

### 1.5 Editing

Six different ways to change something:

| Pattern | Where |
|---|---|
| A separate full form page | Proxy hosts, Backup policies |
| A row turns into inputs | Nodes on the dashboard |
| A row of inputs on the page | New service (dashboard), Time zone (System) |
| A hidden panel that opens | Probe editor (service page) |
| A modal dialog | Connections |
| The browser's `prompt()` | Naming a push device |

On top of that there are **three save concepts**, and the page rarely says which one applies:

* Registry edits change the database at once and need **Save to YAML** to survive a reset; the
  button sits on 9 pages, next to **Reset to saved**, which throws away everything not saved.
* Proxy host changes also need **Push** to Traefik Manager (a banner on one page).
* Connections and system settings apply immediately with no save step.

Destructive actions use the browser's `confirm()` (8 on the service page, 6 on System, 4 on
Proxy hosts), with no indication of what else is affected and no undo beyond the archive.

### 1.6 Feedback and messages

* The shared `toast()`/`apiFetch()` in `app.js` exists, but it is suppressed in 129 places; 16
  pages define their own `setMsg()` (190 calls) writing into a per-section message line.
* Helpers are copy-pasted: `$` is defined in 16 files, `el` in 22, `setMsg` in 16, `cell` in 4.
  Each copy is slightly different.
* Long explanatory paragraphs at the top of most pages (Notifications, Data sources, System)
  push the content below the fold. Proxy hosts already folds its help into a "How this page
  works" disclosure; nothing else does.
* Stale guidance after the Connections move: Notifications still says to set `NTFY_URL` /
  `NOTIFIER_ADAPTER` in `.env`; the proxy host form says to set `TRAEFIK_REDIRECT_MIDDLEWARE`.

### 1.7 Visual consistency

* 63 distinct hex colours, 265 of them written in templates; `app.css` defines only 10 custom
  properties. Status colours differ per page.
* The badge vocabulary has grown per feature: `healthy`, `ok`, `up`, `failed`, `down` (blue!),
  `unknown`, `disabled`, `off`, `warn`, `pending`, `unchecked`, `incomplete`.
* Buttons: one row on the dashboard has *Edit, Check, Wake, Power off, Restart, force, Delete*;
  the service page has two identical Start/Stop/Restart rows ("This service" and "Its guest").
  There is no rule for which action is primary.
* Tables: only 5 pages have a filter box; no table can be sorted (only the Map has a sort
  option) and none paginates.
* Phone: the sidebar folds into a menu, but the dashboard is 580px wide on a 390px screen, and
  the top of every page on a phone is Save to YAML rather than status and the actions you came
  for (wake, check).

---

## 2. What good looks like

Five rules, then the components they need.

1. **Navigate by what you manage**, not by which system it comes from. Every object has *one*
   home with a list and a detail page; other pages link to it instead of repeating it.
2. **One way to edit**: an object is shown read-only; *Edit* opens the same side panel (drawer)
   everywhere, with inline field errors and Save / Cancel. Creating uses the same panel.
3. **One save model**: a change is applied when you press Save, full stop. Persisting to
   `services.yaml` and pushing to Traefik Manager happen automatically after it, and anything
   still pending shows in **one** place (a header indicator), never as buttons on every page.
4. **Update in place**: a page is made of regions; a live event refreshes the regions it
   concerns, without a reload. Nothing you are typing or have open is ever thrown away.
5. **Fast first paint**: pages show the last known state immediately (what the scanners already
   collected), say how old it is, and refresh live data behind it.

### 2.1 Proposed navigation

```
Overview        Dashboard              problems first, then nodes, then recent activity
                Map
Services        Services               list: search, filter by state/group/node, sort
                (service detail)
Infrastructure  Nodes & guests         nodes, guests, power, health (was dashboard + Guests tab + Data sources/Proxmox)
                Proxy hosts            tabs: Hosts · Import · Format (was 3 pages)
                Containers             Portainer stacks and containers, per connection
                Backups                tabs: Policies · Snapshots · Dumps · Orchestrator backups
Activity        Activity               runs, changes, alerts (one feed with filters)
                Traffic                tabs: Over time · By country
Settings        Connections            adapters + connection pages (was Connectivity tabs)
                Notifications          channels, rules, devices
                AI assistants
                Admins & devices       (was the bottom of the dashboard)
                System                 version, update, time zone, maintenance
                Archive
```

16 entries become 15, but each now answers one question, and Data sources disappears as a
place: its import actions move to the list they import into (Import tab on Proxy hosts, "Import
from Proxmox" on Nodes & guests, "Adopt" on Containers). Old URLs redirect, so notification
links keep working.

### 2.2 Page templates

Every page uses one of three layouts:

* **List**: page header (title, count, one primary action), filter bar (search + chips), table
  with sortable columns and a row menu (`⋯`) for secondary actions, bulk bar when rows are
  selected, empty state, phone view as cards.
* **Detail**: header with name, status badge, primary actions (Wake, Check...) and an `⋯` menu
  for the rest; tabs *Overview · Configuration · Related · History*; a facts column on the right
  (node, guest, address, policy, last backup) on wide screens. Configuration is read-only with
  Edit opening the drawer.
* **Settings**: a short form per card, saving per card, help folded into a disclosure.

### 2.3 Shared components (replacing the copies)

| Component | Replaces |
|---|---|
| `ui.js`: `$`, `el`, `fmt.time/bytes/ago`, `api` | 16 `$`, 22 `el`, 4 `cell`, 3 time formats |
| `Notice` (toast for results, inline for field errors, banner for state) | 16 `setMsg` variants, 129 suppressed toasts |
| `Drawer` form (from a field schema, like Connections already does) | 6 editing patterns, `prompt()` |
| `Confirm` dialog with consequences and typed confirmation for destructive actions | native `confirm()` |
| `DataTable` (search, sort, filter chips, row menu, bulk, pagination, phone cards) | every hand-built table |
| `Region` (`load`, live topics, "updated Xs ago", skeleton, error state) | `location.reload()`, per-page timers |
| `Tabs` (URL-addressable, one implementation) | 3 tab implementations |
| Design tokens in `app.css` (colour, status, spacing, radius, type) + one status vocabulary: `ok` `warn` `fail` `off` `unknown` `pending` | 63 colours, 12 status names, 17 `<style>` blocks |

No framework and no build step: the Pi deploy, the single image and the plain Jinja + JS stay.
The components are small vanilla modules, loaded once and cached.

### 2.4 Speed and refresh, concretely

* **Last-known state first.** A small server-side cache per adapter read (10 to 30 s TTL,
  invalidated on writes), shared by pages, the scanners and the collectors. Responses carry
  `as_of`; the region shows "as of 14:02 · refreshing" and swaps in fresh data when it arrives.
* **Slow parts stream in.** Node status returns per node instead of waiting for the slowest;
  the service page renders its sections independently.
* **Cacheable assets.** Page scripts move out of the templates into `/static`, served with a
  version in the URL (git sha) and `Cache-Control: immutable`. Templates shrink to markup.
* **One fewer call per page.** Display settings (timezone, formats) go into the base template.
* **Refresh by region.** Each region subscribes to its live topics; no page reloads itself, so
  `shouldSkipAutoReload` and its six special cases go away. Hidden tabs pause.

---

## 3. Plan

Each phase ships on its own and leaves the UI working. Phases 1 and 2 are invisible
foundations; from phase 3 on, pages move to the new layout one at a time.

| Phase | What | Visible result |
|---|---|---|
| **1. Foundations** | Design tokens + status vocabulary in `app.css`; `ui.js` (helpers, Notice, Confirm, Drawer, Tabs, Region, DataTable); versioned immutable static files; display settings in the base template | Nothing yet; every later page uses these |
| **2. Speed** | Adapter read cache with `as_of`; per-node streaming status; move inline page scripts to `/static` | Pages open faster, repeat visits much faster |
| **3. Navigation** | New sidebar and URLs with redirects; Services list page; Admins & devices out of the dashboard; header indicator for unsaved registry changes and pending pushes (replaces 9 Save to YAML / Reset buttons) | The new map of the app |
| **4. Service detail** | Rebuilt on the Detail template: header actions, tabs, facts column, drawers for service/probe/proxy/backup/deploy settings, regions with live refresh (no reloads) | The busiest page, calm |
| **5. Lists** | Services, Nodes & guests, Proxy hosts, Containers, Backups on DataTable + Drawer; Import/Format as tabs; Data sources removed | Every list behaves the same |
| **6. Settings** | Connections, Notifications, AI assistants, System, Archive on the Settings template; stale `.env` guidance removed | Configuration in one place |
| **7. Dashboard and phone** | Dashboard rebuilt as "what needs attention": problems, nodes, recent activity, quick actions; phone card layouts | The front page answers "is everything fine?" |

Tests: each phase keeps the existing API tests green (the API does not change) and adds
Playwright checks per page: renders without script errors, no horizontal scroll at 390px, live
event updates a region without a reload, drawer save and cancel.

### Decisions needed before phase 3

1. **Auto-save to `services.yaml`**: write the YAML (with a backup copy) after every applied
   change, instead of a Save to YAML step. Recommended: yes; Reset becomes "restore a previous
   version" from the backups list.
2. **Auto-push proxy hosts** to Traefik Manager after Save, instead of a separate Push.
   Recommended: yes, with the pending indicator as the fallback when the push fails.
3. **The navigation in 2.1**, in particular removing Data sources as a page.
