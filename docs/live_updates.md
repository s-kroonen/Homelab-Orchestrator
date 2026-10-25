# Live updates

Pages used to reload themselves every 30-60 seconds. They now update when something changes,
over one Server-Sent Events stream per open page (`GET /api/events`, admin login required). The
small dot above your name in the sidebar is green while the stream is open.

## How it works

* **The source is the database.** After a commit that wrote a pipeline run (wake / backup /
  restore / on-off / deploy), a health verdict, an audit row, a notification event, or a service /
  probe / proxy host / node, an event is published (`orchestrator/events.py`). Every pipeline,
  controller and API call is covered with nothing added to them, and a rolled-back write publishes
  nothing.
* **Events are hints, not state.** `run`, `health`, `audit`, `alert`, `registry`, `mcp`, each with at
  most an id (`service_id`, `run_id`, a verdict). A page that gets one re-reads what it shows from
  the API; one that missed one loses nothing. A client that cannot keep up loses its oldest events.
* **The browser side** is `static/live.js` (`OrchLive.on(topic, fn, {wait})`, debounced), loaded
  on every page.

## What reacts

| Page | On an event |
|---|---|
| Service page | reloads when a run, verdict or edit concerns this service or one under it; the *On / off & deploys* tab refreshes its state table and deploy history in place |
| Dashboard, Map | re-read when any verdict, run or registry change arrives |
| Activity | shows a "New activity - show it" bar (the list never moves under you) |
| Notifications | the feed reloads on a new alert |
| AI assistants, approval page | refresh on any call, connection or request |

Each keeps its old timer as a **fallback**: every 30-60 s while the stream is not connected, and
every 5-10 minutes when it is - because a change made by the CLI runs in another process and cannot
push (`orchestrator-cli deploy run` is seen at the next timer, or when the server itself writes the
verdicts and run rows that follow).

## Behind a reverse proxy

The stream is a response that never ends. If the dot stays grey, something is buffering it:

* **Traefik**: nothing to do by default. Do not put a `compress` middleware on this router (it
  buffers); a buffering middleware or a very short `respondingTimeouts.idleTimeout` on the entry
  point would also cut it. The stream sends a comment every 15 seconds.
* **nginx**: `proxy_buffering off;` (the response already sends `X-Accel-Buffering: no`).
* Cloudflare and similar proxies may cut idle streams or buffer; the page then simply falls back
  to its timer.
