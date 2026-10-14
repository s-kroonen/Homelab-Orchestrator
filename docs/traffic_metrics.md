# Traffic metrics

Per-service and per-host traffic — requests/min, a trend sparkline, and a
2xx/3xx/4xx/5xx breakdown — on the dashboard's **Traffic** page
(`/traffic`), sourced entirely from Traefik's own Prometheus metrics
endpoint and stored in this app's own database. There is no external
Prometheus or Grafana involved or assumed: this is fully self-contained in
the orchestrator.

## Required on the Traefik side

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

## Configuration

Gated entirely by `TRAEFIK_API_URL` (empty = disabled, same as drift
detection) — there is no separate on/off flag. Two more settings control the
collector:

| Setting | Default | Meaning |
|---|---|---|
| `TRAEFIK_METRICS_POLL_INTERVAL_S` | `60` | How often the collector polls `{TRAEFIK_API_URL}/metrics`. |
| `TRAEFIK_METRICS_RETENTION_DAYS` | `14` | How long samples are kept before being pruned. |

## What gets collected, and how

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
   delta**. A reader (the `/traffic` page) computes a rate as
   `(b.requests_total - a.requests_total) / (b.sampled_at - a.sampled_at)`
   between any two samples it wants to compare, which is robust to a missed
   poll (a longer window, not wrong data) and needs no "first sample ever"
   special case.
5. A decrease between two consecutive samples means Traefik's own counters
   reset — it restarted. The page treats that as "no rate available," never
   as negative traffic.

A poll failure (Traefik down, unreachable, `/metrics` not enabled, timeout,
malformed body) is logged and skipped — never raised — exactly like every
other Traefik interaction in this app.

## Retention

Samples older than `TRAEFIK_METRICS_RETENTION_DAYS` are pruned as part of
every poll (no separate schedule). Pruning runs even on a poll that could
not reach Traefik at all, so a Traefik outage doesn't also stop old samples
from being cleaned up.

## The `/traffic` page

Shows:

- An overall figure: total requests/min across every host with a
  computable rate right now (sum of each host's own two-most-recent-sample
  delta, converted to req/min).
- A per-service, per-host breakdown, grouped by `Service.group` (the same
  grouping the main dashboard uses) — each host's current rate, a sparkline
  of its last ~20 polls, and a 2xx/3xx/4xx/5xx bar for the most recent
  interval.
- Clear empty states: Traefik not configured at all; configured but no
  samples yet (needs at least one more poll); a host with no samples or no
  computable rate (just added, or Traefik restarted since its last sample).

Nothing on this page talks to Traefik directly — it only reads what
`TrafficCollector` has already stored, the same separation dashboard pages
in this app already keep from their own background jobs.

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
