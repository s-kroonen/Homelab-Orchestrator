# GeoIP traffic visibility

Which countries and IPs are hitting this homelab's exposed services, and
against which service — an opt-in companion to
[traffic_metrics.md](traffic_metrics.md)'s per-router request counters,
sourced from a different place: Traefik's own **access log**, not its
aggregate Prometheus counters.

**On the dashboard: the Countries side of `/traffic`.** Next to the "By
service" table the Traffic page shows a **Countries** card (a summary line
with total hits and the local vs external share, then each country with its
flag, a share bar and its hits; private/LAN addresses are "Local network")
and a **Top client addresses** card. The "Visitors" tile counts the distinct
client addresses (up to 50, shown as "50+"). Both follow the page's service
select (`?slug=`) and time controls (`?range=15m…14d`): the first paint is
server-rendered over the same aggregation as the read endpoint below
(`build_geo_view` in `web/routers/geo_traffic.py`), and every refresh
re-fetches `GET /api/traffic/geo` and redraws them in place. With
`GEOIP_ENABLED` off the card says so and points here. The old
`/traffic/geo` page redirects to `/traffic`, keeping `slug` and `range`.

> **Since the access-log pipeline was unified**, GeoIP samples come from the
> same `AccessLogCollector` that feeds `/traffic` — one tail, one cursor —
> and `TRAEFIK_API_URL` is no longer needed for host attribution. The
> sections below describe the GeoIP-specific parts; the shared ingestion
> (gateway requirements, attribution, status) is documented once in
> [traffic_metrics.md](traffic_metrics.md#primary-source-traefiks-access-log-through-traefik-manager).

## Why a separate feature from `traffic_metrics.md`

`TrafficCollector` polls `{TRAEFIK_API_URL}/metrics` — Traefik's aggregate,
cumulative-since-process-start Prometheus counters. They have **no
per-request client IPs at all**, by design (that's not what Prometheus
counters are for). The only place a client IP appears is Traefik's access
log, one line per request.

This app has no direct SSH/filesystem access to that log, and deliberately
does not gain any for this feature. Instead it reuses a read Traefik
Manager (the same tool already providing the write path in
[proxy_hosts.md](proxy_hosts.md)) already exposes for its own **Logs**
tab: `GET /api/traefik/logs?lines=N` (capped at 1000 lines), gated on an
access-log path being configured on the gateway. That is
`ProxyAdapter.tail_access_log` (`orchestrator.adapters.proxy.base`),
implemented by `TraefikManagerProxyAdapter` and faked (as `None`, never a
real network call) by `DryRunProxyAdapter`.

## Opt-in, and why

`GEOIP_ENABLED` (default `false`) gates the GeoIP half — writing
`GeoTrafficSample` rows, the GeoIP database, the read endpoint's real
response. The access-log tail itself runs regardless (it powers `/traffic`;
see `ACCESS_LOG_ENABLED`). Off by default
because:

- It is a brand-new integration nobody has opted into yet, same "off by
  default" convention `HEALTH_SCAN_ENABLED`/`SCHEDULER_ENABLED` already
  follow.
- It needs `TRAEFIK_MANAGER_URL` configured AND an access-log path set on
  the gateway (`ACCESS_LOG_PATH` or Traefik Manager's own Settings) — most
  installs following this repo's existing docs will not have the latter
  yet.
- It downloads and caches a third-party GeoIP database (see below) the
  first time it runs — not something to do silently.

With it off, no GeoIP samples are written (the shared tail still feeds
`/traffic`), and `GET /api/traffic/geo` answers `{"enabled": false, ...empty...}` without
touching the database at all.

## Ingestion path

```
Traefik's access log file
        │  (tailed internally by Traefik Manager for its own Logs tab)
        ▼
Traefik Manager  GET /api/traefik/logs?lines=N
        │  (ProxyAdapter.fetch_access_log — surfaces TM's own "error" text)
        ▼
orchestrator.scheduler.access_log_collector.AccessLogCollector
        │  parse (orchestrator.registry.access_log)
        │  dedupe against a persisted cursor
        │  attribute to a ProxyHost (RequestHost, else router name via
        │    TM /api/traefik/routers + /api/routes, else TRAEFIK_API_URL)
        ├──────────────────────────────► traffic_rollup (per minute, /traffic)
        │  GEOIP_ENABLED only:
        │  resolve IP -> country (orchestrator.registry.geoip)
        │  aggregate by (proxy_host, client_ip)
        ▼
GeoTrafficSample rows
        │
        ▼
GET /api/traffic/geo  (read-only; same aggregation as the /traffic/geo page)
```

Nothing here is a new SSH, filesystem, or direct-Traefik-API integration —
it is one more REST call to the same Traefik Manager instance
`TRAEFIK_MANAGER_URL` already points at.

## Access-log format(s) supported

Traefik's access log has exactly two shapes (its own docs, "Logs and
access logs"): `common` (an extension of the Apache/NCSA Combined Log
Format with four extra, Traefik-specific fields appended) — Traefik's own
**default** when `accessLog` is enabled with no `format` given — and
`json`, one object per line under Go field names.

`orchestrator.registry.access_log.parse_line` supports **both** — sniffing
each line (`{` => JSON, otherwise the common-format regex) rather than
requiring the operator to tell this app which one Traefik is configured
with, since that is static config on the gateway this app has no visibility
into otherwise. Supporting both turned out to cost about the same as
supporting one (each parser is a handful of lines), so there was no reason
to pick only one.

JSON is recommended: it carries `RequestHost`, which attributes a line to a
proxy host directly.

A line that matches neither shape (truncated, a future/unknown format,
blank, non-log noise) is reported back as `None`. The collector counts it
(`unparsable` in its status; a WARNING when nothing in a tail parses) and
skips it — **a single garbled line never crashes the
poll**, same fail-open philosophy as every other optional integration in
this app.

## Dedup / cursor strategy

`GET /api/traefik/logs` has **no native "since" parameter** — every poll
re-fetches the same tail (the last `ACCESS_LOG_LINES` lines of the
*current* file), per Traefik Manager's own API docs. Re-polling therefore
re-fetches lines this collector may already have counted; naively counting
every fetched line every time would corrupt the aggregate numbers (the same
risk `TrafficCollector` avoids by storing a raw cumulative counter instead
of a delta — this collector's situation is different because there is no
counter at all here, only a sliding window of raw lines).

The fix: a cursor persisted in the `setting` table
(`geoip_traffic.cursor`, via `orchestrator.db.models.Setting` — the same
table the YAML-reconcile loader already uses for its own bookkeeping
markers) holding:

- `last_ts`: the latest timestamp seen across every *parsed* line in the
  previous poll's fetched tail.
- `last_hashes`: the SHA-256 hashes of every line that shared that exact
  timestamp (ties are possible under load — Traefik's own access-log
  timestamp resolution is not guaranteed sub-line-unique).

A line counts as "new" this poll if its timestamp is strictly after
`last_ts`, or exactly equal to it and its hash isn't in `last_hashes`. The
cursor is then advanced using **every** line in the current poll's fetched
tail (not just the newly-counted ones), so a poll that sees nothing new
still keeps the cursor correct for the next one.

Persisting it (rather than keeping it only in the collector's own memory)
means a container restart does not re-count whatever is still sitting in
the tail window — verified by `tests/test_access_log_collector.py`'s
`test_cursor_is_persisted_in_the_setting_table`, and the double-counting
regression itself by `test_repolling_the_same_tail_does_not_double_count` /
`test_repolling_an_overlapping_but_grown_tail_only_counts_new_lines`.

**Accepted gap, not a bug:** if more than `ACCESS_LOG_LINES` new
lines land between two polls, the oldest of them fall outside the tail
entirely and are permanently missed — there is no way to recover them
without reading the file directly, which is deliberately out of scope (see
`ProxyAdapter.tail_access_log`'s docstring). This mirrors
`TrafficCollector`'s own accepted "up to one poll interval of latency"
trade-off: good enough for trend visibility, not a guarantee of exact
counts.

## Local/private IPs get their own bucket

A homelab's exposed services see plenty of RFC 1918 / loopback /
link-local / CGNAT / multicast traffic (the LAN itself, health checks,
internal probes) that no public GeoIP database can resolve to a country —
and that is expected, not a lookup failure. `orchestrator.registry.geoip`
buckets these explicitly as `"local"` (via Python's own `ipaddress`
module: `is_private`/`is_loopback`/`is_link_local`/`is_multicast`/
`is_reserved`/`is_unspecified`), distinct from `"unknown"` (a genuinely
public IP the configured database just doesn't cover). This check runs
**before** any database lookup, so it works even with GeoIP otherwise
unavailable.

## GeoIP database

`GEOIP_DB_PATH` points at a ready-made MaxMind-format (`.mmdb`) file. Left
empty (the default), `orchestrator.registry.geoip.GeoIP` downloads and
caches the free **DB-IP city-lite** database under `{STATE_DIR}/geoip/` —
the same database Traefik Manager's own GeoIP feature downloads
(`POST /api/geoip/update` in its API docs) when no custom database is
configured, rather than this app inventing a different source or
dependency. The cache is keyed by calendar month (DB-IP republishes
monthly); a stale cached file from a previous month is still used if a
fresh download fails, rather than going dark for a transient network
hiccup. A `GEOIP_DB_PATH` that is set but does not exist on disk is
**not** auto-downloaded over — that would silently override an explicit
operator choice — it is logged and GeoIP resolves as `"unknown"` until
fixed.

Resolution uses [`geoip2`](https://pypi.org/project/geoip2/), the standard
Python client for MaxMind-format databases (DB-IP's own files are
MaxMind-DB-compatible). `GeoIP` detects whether the configured database is
city-capable (via its own metadata) and calls `.city()` for a city name too
when it is, falling back to `.country()` otherwise.

## Storage

`GeoTrafficSample` (`orchestrator.db.models`) — the GeoIP companion to
`TrafficSample`, same compact/short-retention philosophy as
`HealthScanSample`: one row per `(proxy_host, client_ip)` pair actually
seen in a poll cycle, with `hit_count` holding how many *new* access-log
lines (since the cursor) matched it, plus the resolved `bucket` /
`country_code` / `country_name` / `city` and the most recent `status_code`
observed for that pair this cycle. This is a monitoring trend view
("which countries/IPs hit us, how often, against which service"), not a
forensic per-request log — the raw lines already live in Traefik's own
access-log file; this does not duplicate them.

Only entries that resolve to an existing `ProxyHost` are stored (request
host, else router name through Traefik Manager, else `TRAEFIK_API_URL` —
see [traffic_metrics.md](traffic_metrics.md#how-a-line-is-attributed-to-a-proxy-host))
— a line with no matching `ProxyHost` has no `Service` to attribute the hit
to. It is counted under its router in the collector status instead.

Migration: `0008_geo_traffic_sample` (`down_revision =
"0007_health_scan_sample"`, the actual current head at the time this was
written).

## Retention

`GEOIP_TRAFFIC_RETENTION_DAYS` (default **14**, matching
`TRAEFIK_METRICS_RETENTION_DAYS`) — pruned every poll, same pattern as
every other collector here, independent of whether that particular poll
got anything new.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `GEOIP_ENABLED` | `false` | Gates the whole feature. |
| `GEOIP_DB_PATH` | `""` | A ready-made `.mmdb` file. Empty = auto-download/cache the free DB-IP city-lite database. |
| `GEOIP_TRAFFIC_POLL_INTERVAL_S` | empty | **Deprecated** alias for `ACCESS_LOG_POLL_INTERVAL_S` (default 30), used only when that is unset. |
| `GEOIP_ACCESS_LOG_LINES` | empty | **Deprecated** alias for `ACCESS_LOG_LINES` (default 1000), used only when that is unset. |
| `GEOIP_TRAFFIC_RETENTION_DAYS` | `14` | How long `GeoTrafficSample` rows are kept. |

Also requires (already-existing settings, shared with
[proxy_hosts.md](proxy_hosts.md)'s write path): `TRAEFIK_MANAGER_URL` +
`TRAEFIK_MANAGER_API_KEY`, and an access-log path configured on the
gateway side (Traefik Manager's `ACCESS_LOG_PATH` or Settings UI), plus
`PROXY_ADAPTER=traefik_manager` with `DRY_RUN=false` and
`ACCESS_LOG_ENABLED=true` (default). When the path is missing or refused,
Traefik Manager's own message is shown on both traffic pages instead of an
unexplained empty page.

## The one read endpoint

`GET /api/traffic/geo?range=<15m|1h|6h|24h|7d|14d>&slug=<optional>`
(admin-only, same `require_admin` gate as every other `/api/*` router) —
country/IP counts aggregated (in SQL) over samples whose `sampled_at` falls
in the range (default `24h`):

```json
{
  "enabled": true,
  "range": "24h",
  "countries": [
    {"country_code": "US", "country_name": "United States", "bucket": "country", "hits": 42},
    {"country_code": "", "country_name": "", "bucket": "local", "hits": 17}
  ],
  "top_ips": [
    {"client_ip": "8.8.8.8", "bucket": "country", "country_code": "US", "hits": 42}
  ],
  "sample_count": 2
}
```

`?slug=` narrows to one service's proxy hosts; omitted, every proxy host's
samples are summed. `{"enabled": false, ...empty...}` when `GEOIP_ENABLED`
is off. The Countries card on `/traffic` renders this same aggregation
(`build_geo_view`), so the page and the endpoint never disagree.

## Failure handling

Every step degrades, logs, and skips rather than raising or crashing the
poll:

- Access log unconfigured/unreachable, or Traefik Manager answers with an
  `error` -> recorded in the collector status (shown on both pages, logged
  at WARNING); pruning still runs.
- An unparsable line -> `None` from `parse_line`, counted, skipped.
- GeoIP database missing/unavailable -> every non-local IP resolves as
  `"unknown"`, never a raised exception.
- A router with no matching `ProxyHost` -> that line's hit is simply not
  stored (nothing to attribute it to).

Same fail-open philosophy as every other optional integration in this app.
