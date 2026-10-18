# Traffic metrics

Per-service and per-host traffic on the dashboard's **Traffic** page
(`/traffic`): requests over time stacked by status class (2xx/3xx/4xx/5xx),
average and maximum latency, and a per-service table with hits, error
counts/rates, latency and a hits sparkline — all over a chosen time range.
Everything is stored in this app's own database; there is no external
Prometheus or Grafana involved.

## Primary source: Traefik's access log, through Traefik Manager

`/traffic` (and `/traffic/geo`, see [geoip_traffic.md](geoip_traffic.md))
are fed by **one** ingestion pipeline: `AccessLogCollector`
(`orchestrator/scheduler/access_log_collector.py`) tails Traefik's access log
through Traefik Manager's `GET /api/traefik/logs?lines=N` every
`ACCESS_LOG_POLL_INTERVAL_S`, dedupes it against a persisted cursor, attributes
each line to a proxy host, and writes:

- one `traffic_rollup` row per proxy host per UTC minute (hits, 2xx/3xx/4xx/5xx
  counts, duration sum/count/max) — always; and
- `geo_traffic_sample` rows — only when `GEOIP_ENABLED=true`.

**`TRAEFIK_API_URL` is not needed.** Operators who run Traefik Manager as the
source of truth and left `TRAEFIK_API_URL` empty get full traffic pages. (The
older Prometheus collector still runs when it *is* set; see
[below](#optional-prometheus-counters-traefik_api_url).)

### What has to be true on the gateway

1. **Traefik writes an access log.** In Traefik's static config:

   ```yaml
   accessLog:
     filePath: /var/log/traefik/access.log
     format: json        # recommended — see below
   ```

2. **Traefik Manager can read it.** Set `ACCESS_LOG_PATH` on the Traefik
   Manager container (or the access-log path in its Settings) to that same
   file, mounted read-only into the container. Traefik Manager's own Logs
   tab should show lines once this is right.

3. **This orchestrator talks to a live Traefik Manager:**
   `PROXY_ADAPTER=traefik_manager`, `TRAEFIK_MANAGER_URL`,
   `TRAEFIK_MANAGER_API_KEY`, `DRY_RUN=false`, and `ACCESS_LOG_ENABLED=true`
   (the default).

**JSON format is recommended.** Both Traefik formats parse, but only JSON
carries `RequestHost` (the request's own `Host`) and a precise `Duration`
(nanoseconds). The common format only names the router and ends with a Go
duration string (`12ms`, `1.5s`, `350µs`), which is parsed too.

### How a line is attributed to a proxy host

In order, with the router map fetched at most once per poll:

1. **Request host** — JSON `RequestHost` (port stripped) matched against
   `ProxyHost.hostname`.
2. **Router name** — the log's `RouterName` (e.g. `jellyfin@docker`) is
   resolved to hostnames through Traefik Manager. Primary:
   `GET /api/traefik/routers`, which proxies Traefik's *runtime* routers for
   every provider (docker, file, …) under the exact `name@provider` names the
   access log uses, with their rules. Also: `GET /api/routes`, the
   file-provider routes Traefik Manager manages, listed by bare name. Names
   are compared with the `@provider` suffix stripped on both sides.
3. **Traefik API** — `fetch_router_details` against `TRAEFIK_API_URL`, only
   when that is set.

A line that matches no proxy host is counted (per router) in the collector
status and otherwise dropped — a router with no proxy host has no service to
attribute it to. Fix: add the proxy host, usually via **Proxy hosts →
Resync**.

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `ACCESS_LOG_ENABLED` | `true` | Run the collector (only does anything with a live Traefik Manager proxy adapter). |
| `ACCESS_LOG_POLL_INTERVAL_S` | `30` | How often the log is tailed. |
| `ACCESS_LOG_LINES` | `1000` | Lines requested per poll (Traefik Manager's maximum). |
| `TRAFFIC_ROLLUP_RETENTION_DAYS` | `14` | Per-minute rollups older than this are pruned each poll. |

`GEOIP_TRAFFIC_POLL_INTERVAL_S` / `GEOIP_ACCESS_LOG_LINES` are deprecated
aliases, honoured only when the `ACCESS_LOG_*` name is not set.

If more than `ACCESS_LOG_LINES` new lines land between two polls, the oldest
fall outside the tail and are missed — shorten the interval on a busy proxy.
This is a trend view, not exact accounting.

### Reading the collector status

Both traffic pages show the last poll's outcome above the controls
(also `GET /api/traffic/collector-status`, admin-only; persisted in the
`access_log.status` setting after every poll):

- **Strip** — `● Last poll 12s ago · 184 lines · 180 matched · 4 unmatched`:
  healthy. "lines" is the fetched tail, "matched" how many of its parsed lines
  resolved to a proxy host (hover "unmatched" for the routers).
- **Red notice, "Traefik Manager could not provide the access log."** —
  quotes Traefik Manager's own error, e.g. *Access log not found. Set
  ACCESS_LOG_PATH env var or configure the path in Settings.* or *Access log
  path refused: …*, or a connection error. Fix steps 1–3 above.
- **Amber notice, "None of the N access-log lines matched a proxy host."** —
  lists the router names seen; each needs a proxy host (**Proxy hosts →
  Resync**).
- **Amber, "could not be parsed"** — the lines are in neither Traefik format;
  set `accessLog.format: json`.
- **Amber, "came back empty"** — Traefik Manager found the file but it has no
  lines; check that Traefik is writing it.
- **Blue, "not running"** — the proxy adapter is dry-run or Traefik Manager is
  not configured.

The collector also logs at WARNING when Traefik Manager returns an error,
when nothing in a non-empty tail parses, and when nothing matches.

## The `/traffic` page

- **Time controls** (shared with `/traffic/geo`, `static/timecontrols.js`):
  range (last 15 m / 1 h / 6 h / 24 h / 7 d / 14 d), auto-refresh
  (off / 10 s / 30 s / 1 m / 5 m), a ↻ button and "updated Xs ago". The state
  lives in the URL (`?range=1h&refresh=30s`) so reloads and shared links keep
  it; the browser remembers your last choice for bare visits. Refreshing
  re-fetches JSON and redraws in place (never a page reload) and pauses
  while the tab is hidden.
- **Stat tiles**: requests, 4xx, 5xx (count and share), average latency.
- **Requests by status** — stacked columns per bucket, 2xx (blue) / 3xx
  (aqua) / 4xx (yellow) / 5xx (magenta), with the range totals in the legend.
- **Latency** — average and maximum per bucket (gaps where nothing was timed).
- Hover (or focus + arrow keys) shows the bucket's time span and values.
- **By service** — every proxy host with hits, 4xx/5xx (count + rate),
  avg/max latency and a hits sparkline. Click a row to scope the charts to
  that service (`?slug=`); click it again or the chip's × to clear.

Bucket size follows the range: ≤ 1 h → 1 min, 6 h → 5 min, 24 h → 15 min,
7 d / 14 d → 1 h. Empty buckets are zero-filled.

### `GET /api/traffic/series?range=1h&slug=`

```json
{
  "enabled": true, "range": "1h", "slug": null, "step_s": 60,
  "from": "…Z", "to": "…Z", "buckets": ["…Z", …],
  "overall": {"hits": [], "s2xx": [], "s3xx": [], "s4xx": [], "s5xx": [],
              "avg_ms": [number|null], "max_ms": [number|null]},
  "totals": {"hits", "s2xx", "s3xx", "s4xx", "s5xx", "avg_ms", "max_ms"},
  "hosts": [{"proxy_host_id", "hostname", "service_slug", "service_name",
             "totals": {"hits", "s4xx", "s5xx", "avg_ms", "max_ms"},
             "hits": [], "s5xx": []}]
}
```

`slug` scopes `overall`/`totals`; `hosts` always lists every proxy host so the
table can switch services. `enabled` says whether the collector is
configured to run. Admin-only.

## Optional: Prometheus counters (`TRAEFIK_API_URL`)

The original collector, `TrafficCollector`, still runs when
`TRAEFIK_API_URL` is set. Its samples are no longer what `/traffic` draws
(the access-log rollups are), but it keeps feeding the wake-on-hit stand-in
below. Traefik Manager does not proxy `/metrics`, so this path needs direct
access to Traefik's API.

### Required on the Traefik side

Start Traefik with:

```yaml
metrics:
  prometheus: {}
```

(or the equivalent `--metrics.prometheus=true` CLI flag / static-config
line). This serves a plain-text metrics page at `GET /metrics` on the same
entrypoint `TRAEFIK_API_URL` already points at for drift detection and
resync (see [proxy_hosts.md](proxy_hosts.md)) — no separate port or
entrypoint is needed, and no separate credential: the same
`TRAEFIK_API_VERIFY_TLS` / `TRAEFIK_API_USERNAME` / `TRAEFIK_API_PASSWORD` /
`TRAEFIK_API_TIMEOUT_S` settings apply.

### Configuration

Gated entirely by `TRAEFIK_API_URL` (empty = disabled, same as drift
detection) — there is no separate on/off flag. Two more settings control the
collector:

| Setting | Default | Meaning |
|---|---|---|
| `TRAEFIK_METRICS_POLL_INTERVAL_S` | `60` | How often the collector polls `{TRAEFIK_API_URL}/metrics`. |
| `TRAEFIK_METRICS_RETENTION_DAYS` | `14` | How long samples are kept before being pruned. |

### What gets collected, and how

Traefik's counters are **cumulative since the Traefik process last
started** — a single snapshot only ever says "total requests so far," not a
rate. Since there is no Prometheus here to keep history, this app has to
poll repeatedly and store what it sees itself; that is the job of
`TrafficCollector` (`orchestrator/scheduler/traffic_collector.py`), a small
APScheduler job separate from the backup scheduler, started from
`web/app.py`'s lifespan alongside it.

On each poll:

1. `GET {TRAEFIK_API_URL}/metrics` is fetched and parsed (a small, forgiving
   line-based Prometheus text parser in
   `orchestrator/registry/traefik_status.py` — not a `prometheus_client`
   dependency, which is for *exposing* metrics, not reading someone else's).
   Only three metric families are read; everything else Traefik exposes is
   ignored:
   - `traefik_router_requests_total` (by `router` and `code`)
   - `traefik_router_request_duration_seconds_sum` (by `router`)
   - `traefik_router_request_duration_seconds_count` (by `router`)
2. `GET {TRAEFIK_API_URL}/api/http/routers` resolves each router to its
   current `Host()`/`HostSNI()` hostnames — the same lookup drift detection
   and Resync already use.
3. Each router's hostname is matched against the existing `ProxyHost` table.
   **A router with no matching `ProxyHost` is skipped — nothing is stored
   for it.** `ProxyHost.service_id` is how traffic gets attributed to a
   `Service`; a router the operator hasn't registered as a proxy host has no
   `Service` to attribute its traffic to, so there is nothing useful to keep
   (the same reasoning Resync uses for its `discovered` list never being
   auto-created — see [proxy_hosts.md](proxy_hosts.md)).
4. One `TrafficSample` row is written per matched router, holding the RAW
   counter values from that poll (`requests_total`, `duration_sum_s`,
   `duration_count`, and a `by_code` JSON map) — **never a precomputed
   delta**. A reader (the wake-on-hit check below) computes a rate as
   `(b.requests_total - a.requests_total) / (b.sampled_at - a.sampled_at)`
   between any two samples it wants to compare, which is robust to a missed
   poll (a longer window, not wrong data) and needs no "first sample ever"
   special case.
5. A decrease between two consecutive samples means Traefik's own counters
   reset — it restarted. A reader treats that as "no rate available," never
   as negative traffic.

A poll failure (Traefik down, unreachable, `/metrics` not enabled, timeout,
malformed body) is logged and skipped — never raised — exactly like every
other Traefik interaction in this app.

### Retention

Samples older than `TRAEFIK_METRICS_RETENTION_DAYS` are pruned as part of
every poll (no separate schedule). Pruning runs even on a poll that could
not reach Traefik at all, so a Traefik outage doesn't also stop old samples
from being cleaned up.
## Wake-on-hit: a stand-in for a real Traefik-side trigger

`POST /wake/{service_slug}` (`web/routers/wake.py`) was designed to be
called by Traefik's own **errors middleware** whenever a visitor hits a
down backend — see that router's docstring and
`orchestrator.web.wake_support`. That requires pushing config on the
Traefik side (an `errors` middleware pointed at this app, attached to the
relevant routers), and the owner currently only has **read-only API
access** to Traefik, which lives on a different host (see
[proxy_hosts.md](proxy_hosts.md)'s Traefik sections). Until write access
exists, injecting a wake trigger directly into Traefik's config is future
work, out of scope here.

As a stand-in, `TrafficCollector` — which is already polling
`{TRAEFIK_API_URL}/metrics` every `TRAEFIK_METRICS_POLL_INTERVAL_S` — also
checks, on every poll, whether traffic landed on a `ProxyHost` whose linked
service currently looks down, and if so fires the exact same
`WakePipeline` that `POST /wake/{slug}` fires (same `get_or_start`
cooldown/dedup, same background execution via
`orchestrator.web.wake_support.fire_background`). **This is knowingly
slower than a real Traefik-side trigger — up to one full poll interval of
latency — which is an accepted trade-off for something that works today
without any Traefik-side config.**

This is a *separate, additive* path. It never touches, overrides or
replaces the manual "Wake now" button on the dashboard, the CLI wake
command, or `POST /wake/{slug}` itself (including Traefik's errors
middleware calling it, once that is eventually wired up) — those stay
exactly as unconditional as they are today. Wake-on-hit runs are recorded
with `actor="traefik_hit"` in the audit trail / `PipelineRun` history, so
they're distinguishable from `actor="api"` (manual/API) and
`actor="scheduler"` (backups).

### The three-way gate

A wake-on-hit only fires when **all three** of these are true for the hit
proxy host's chain:

| Field | Default | Meaning |
|---|---|---|
| `ProxyHost.wake_on_hit` | **False** | Per-route opt-in: does traffic to *this* hostname count as a wake trigger at all. |
| `Service.wake_on_hit_enabled` | **True** | Safety rail: this specific service can be auto-woken this way. |
| `Node.wake_on_hit_enabled` | **True** | Safety rail: this physical node can be auto-woken this way at all — if `False`, nothing wakes it through this path, regardless of any service's or proxy host's own setting. |

**Why these defaults:** `ProxyHost.wake_on_hit` defaults to `False` because
this behaviour did not exist before — defaulting it on would silently
start auto-waking services for every existing installation the moment it
upgrades, which nobody asked for. `Service.wake_on_hit_enabled` and
`Node.wake_on_hit_enabled` are not independent opt-ins; they're rails
layered *on top of* that per-host opt-in, there to let an operator carve
out an exception ("every proxy host on this node opts in, except this one
service" / "nothing on this node, period"). Defaulting those two to `True`
means turning on `wake_on_hit` for a proxy host is enough by itself — the
common case — without also having to flip two more switches elsewhere.

All three are edited where their other toggles already live: `Node` on the
main dashboard's node table, `Service` on its detail page (`/services/{slug}`),
`ProxyHost` on the proxy-hosts page (`/proxy-hosts`) — and they all round-trip
through **Save to YAML** / **Reset to saved** like every other registry field.

None of these three flags affect `POST /wake/{slug}`, the manual button, or
the CLI — see the scoping note above.

### "Currently down," for this purpose

A hit only counts as wake-worthy if the service isn't already up. This
reads `ServiceState.last_verdict` (see `orchestrator.health.engine` for what
each `HealthState` means):

- **`FAILED`** → down. Unambiguous — a required probe definitively failed.
- **No `ServiceState` row at all** (the service has never been scanned) →
  treated as down. There's no evidence it's up, trying a wake is safe (the
  normal `get_or_start` cooldown stops this from being retried on every
  subsequent hit), and it's the honest "we don't know" case most likely to
  appear on a service hasn't had a health check configured yet, or whose
  node was already off since boot.
- **`UNKNOWN` with an existing verdict** → **not** treated as down. Unlike
  a fresh install, an `UNKNOWN` that was actually recorded can mean a
  blocked dependency, a probe/transport config error, or a transient
  network blip — none of which say anything about whether the *node* is
  powered on. Auto-waking a node on that signal is a much more expensive
  mistake here (an unwanted power-on + guest start) than the backup
  gate accepts when it skips a backup on the same `UNKNOWN`.
- **`HEALTHY`** → not down, obviously; traffic to a healthy service is just
  traffic.

### Also required: real, new traffic this poll

A hit only counts if this poll's `requests_total` for that `ProxyHost` is
strictly greater than the immediately preceding sample's — i.e., Traefik
actually served new requests between the two polls, not merely "the host
exists." A proxy host with only one sample ever (nothing to compare
against yet) is skipped that round. A *decrease* is never treated as
traffic either — same rule as everywhere else in this file: it means
Traefik's own counters reset (it restarted), not negative traffic.

### No separate dedup — it reuses the wake pipeline's own cooldown

This feature does not invent its own "don't fire twice" logic.
`WakePipeline.get_or_start` already refuses to start a second run for a
service while one is `RUNNING`, or within `WAKE_RETRY_COOLDOWN_S` of the
last one finishing — exactly the mechanism that already protects
`POST /wake/{slug}` from being hammered by every visitor hitting a down
backend. Wake-on-hit calls that same method, so repeated hits during an
outage reuse the existing run instead of spamming new wake attempts.

### Failure handling

A failure anywhere in the wake-on-hit check (a bad gate, a pipeline error,
an unexpected exception) is logged and skipped — it never breaks sample
storage or pruning for the rest of that poll, the same defensive
philosophy as every other failure mode in this module.
