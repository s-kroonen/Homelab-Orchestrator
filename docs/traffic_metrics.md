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
