# Monitoring

Three moving parts: the Telegraf plugin (collection), InfluxDB 3 (storage), Grafana
(dashboards). Each has one non-obvious trap; they are documented here because they
cost real time.

## Metrics

| Measurement | Tags | Fields |
| --- | --- | --- |
| `seaweedfs_cluster` | `server` | `volume_count`, `ec_shard_count`, `max_volume_slots`, `free_volume_slots`, `volume_server_count`, `data_center_count`, `rack_count`, `volume_layout_count` |
| `seaweedfs_master` | `server` | `is_leader`, `max_volume_id` |
| `seaweedfs_volume_server` | `server`, `datacenter`, `rack` | `volume_count`, `ec_shard_count`, `max_volume_slots`, `free_volume_slots`, `disk_count` |
| `seaweedfs_disk` | `server`, `path`, `disk_type` | `total_bytes`, `used_bytes`, `free_bytes`, `used_percent`, `free_percent`, `volume_count` |
| `seaweedfs_collection` | `collection` | `volume_count`, `readonly_volume_count`, `writable_volume_count`, `size_bytes`, `file_count`, `delete_count`, `deleted_bytes` |
| `seaweedfs_volume` | `server`, `collection`, `volume_id` | `size_bytes`, `file_count`, `delete_count`, `deleted_bytes`, `read_only`, `version`, `disk_id` |

Notes that matter when writing queries:

- every series exists **once per Telegraf pod** (five here), differing only by the
  `host` tag — aggregate with `max()`/`min()` or filter one host
- all sizes are `int64` bytes; percentages are floats
- `seaweedfs_volume` only exists if `detailed_volumes = true` (one series per
  volume, high cardinality)
- volumes created without a collection are reported as `default`, because Telegraf
  drops empty tag values

## InfluxQL examples

Current disk usage (latest row per path):

```sql
SELECT path, used_percent, used_bytes, free_bytes, volume_count, time
FROM (
  SELECT *, row_number() OVER (PARTITION BY path ORDER BY time DESC) AS rn
  FROM "seaweedfs_disk" WHERE time > now() - 1h
) WHERE rn = 1
```

Volume-slot headroom (the constraint that actually bites):

```sql
SELECT server, max("free_volume_slots") AS free_slots,
       max("max_volume_slots") AS slots, max("volume_count") AS used
FROM "seaweedfs_volume_server" WHERE $timeFilter GROUP BY "server"
```

Per-collection size over time:

```sql
SELECT max("size_bytes") AS "size" FROM "seaweedfs_collection"
WHERE $timeFilter GROUP BY time($__interval), "collection" fill(linear)
```

Ingestion rate (bytes added per bucket):

```sql
SELECT non_negative_difference(max("size_bytes")) AS "bytes_added"
FROM "seaweedfs_collection" WHERE $timeFilter
GROUP BY time($__interval), "collection"
```

Reclaimable space before a vacuum:

```sql
SELECT sum("deleted_bytes") AS "reclaimable" FROM (
  SELECT max("deleted_bytes") AS "deleted_bytes" FROM "seaweedfs_collection"
  WHERE $timeFilter GROUP BY "collection"
)
```

Read-only volumes (should always be empty):

```sql
SELECT collection, max("readonly_volume_count") AS readonly
FROM "seaweedfs_collection" WHERE $timeFilter GROUP BY "collection"
HAVING max("readonly_volume_count") > 0
```

## Grafana datasource

Grafana 13.2.4, built-in `influxdb` datasource, **InfluxQL** mode:

| Setting | Value |
| --- | --- |
| URL | `http://influxdb3-querier.influxdb3.svc.cluster.local:8181` |
| Database | `telegraf` |
| HTTP method | POST |
| Custom header | `Authorization` = `Token <token>` |

Two traps:

1. **Do not use SQL mode.** Grafana's SQL query language talks Arrow **Flight SQL
   over TLS**; the InfluxDB 3 querier here serves plaintext gRPC, so every query
   fails with `tls: first record does not look like a TLS handshake`.
2. **InfluxQL mode sends no token by default.** Without the custom
   `Authorization: Token …` header every panel reports
   `the request was not authenticated`.

`grafana/build_dashboards.py` creates exactly this datasource (reading the token
from the cluster secret), then imports both dashboards.

## Dashboards

### SeaweedFS — Cluster Overview (17 panels)

- **Stat row** — disk used % (thresholds 70/85), disk used, disk free (red below
  50 GB), free volume slots (red at 0)
- **Capacity** — used vs free bytes, used % trend with threshold lines, volume
  slots per server, cluster volumes/slots, stored bytes per collection
- **Health** — leader samples, read-only volumes, reclaimable bytes, highest
  volume ID, scrapers table (samples per host), EC shards and layouts

### SeaweedFS — Collections & Growth (14 panels)

- **Stat row** — total stored, total files, volumes, writable volumes
- **Per collection** — bytes, files, volumes, writable volumes, snapshot table
- **Growth** — bytes added and files added per interval

### SeaweedFS — Volumes (7 panels)

Requires `detailed_volumes = true` on the `[[inputs.seaweedfs]]` input (it is opt-in
because it emits one series per volume). It is the dashboard equivalent of the
volume server's own page at `:8080/ui/index.html`, but with history and with the
five Telegraf scrapers merged (`max()` per volume):

- **Stat row** — volumes, free volume slots (red at 0), fullest volume in MiB
  (yellow at 900 MiB, red at the 1000 MiB per-volume limit), largest trash
  (`deleted_bytes`) awaiting vacuum
- **Volume inventory** — one row per volume: collection, volume id, size, files,
  trash, read-only flag, sorted fullest first; the size cell is a gauge that goes
  yellow/red as a volume approaches the limit
- **Volume fill over time** — fullest and average volume size against the limit
- **Files per volume** — file-count growth, which is what drives slot churn

### Telegraf — All Measurements (48 panels)

One dashboard over **every measurement** in the `telegraf` database (130 when it
was built), generated from the live schema by
`grafana/build_all_measurements_dashboard.py`:

- **Summary row** — ingest lines/s, CPU busy, memory used, disk used, free volume
  slots, Telegraf write errors (per node, with thresholds)
- **Host / OS** and **SeaweedFS** open; **Telegraf**, **Kubernetes**, **InfluxDB 3
  (ingest, WAL, query path, memory)**, **async semaphore**, **object store** and
  **tokio** in collapsed rows, so Grafana does not query them until opened
- measurements sharing a unit are merged into one panel (one target each), which
  is why 130 measurements fit in 42 time-series panels
- the generator asserts every measurement appears exactly once and refuses to
  publish otherwise; `--verify` runs every panel query through the datasource

### Panel gotchas worth knowing

- InfluxQL aggregate frames carry **no labels**, so series names arrive as
  `measurement.field { collection: lab }` and `displayName` templating does not
  work. The panels use a `renameByRegex` transformation instead.
- A stat panel **cannot reduce a boolean**, so `last("is_leader")` shows
  "No data"; the dashboard counts leader samples (`count(...) WHERE is_leader =
  true`).
- First paint of a 17-panel dashboard takes 60–90 s here, because each panel runs
  its own InfluxDB 3 query in parallel. Panels render empty until their query
  returns — refreshing afterwards is instant.
- Growth panels spike right after a collector restart: `non_negative_difference`
  across the gap counts the whole catch-up in one bucket.

## Web UIs

SeaweedFS serves a status page per component. They are read-only, unauthenticated
and point-in-time, so they are exposed on the lab LAN by a small nginx proxy
(`helm/seaweedfs-ui-proxy.yaml`) rather than an Ingress: the cluster has no ingress
controller, and the chart has no NodePort/hostPort knobs.

| UI | LAN URL | What it shows |
| --- | --- | --- |
| volume server | http://192.168.0.21:18080/ui/index.html | disk stats, uptime, request sparklines, and the per-volume table (id, collection, data size, files, trash, TTL, read-only, version) |
| master | http://192.168.0.21:19333/ui/index.html | topology, volume layouts, writables per collection |
| filer | http://192.168.0.21:18888/ | bucket/object browser for `/buckets/lab` |

No authentication by design — lab only, do not carry this pattern into anything
reachable from outside the house network. `kubectl -n s3 delete -f
helm/seaweedfs-ui-proxy.yaml` removes it.

## Alerting

Wired up in Grafana unified alerting by `grafana/create_slots_alert.py`:

| Rule | Condition | For | Notification |
| --- | --- | --- | --- |
| SeaweedFS volume slots low | `max(free_volume_slots) < 5` | 5m | Telegram (`telegram-rick`) |

Details:

- The rule lives in the **SeaweedFS** folder, group `seaweedfs-capacity`, and
  evaluates every minute. It queries `seaweedfs_cluster` directly (`GROUP BY
  time(1m) fill(previous)` over a 15m window), so it does not depend on a
  dashboard being open.
- The annotation carries the live value (`{{ $values.B }}`) plus the fix —
  raise `master.maxVolumes` (30 GB per volume, so ~330 GB of PVC headroom for 11
  slots) and `helm upgrade seaweedfs --atomic --wait`.
- The contact point reuses the Hermes Telegram bot (token read from
  `~/.hermes/.env`, never committed); the default notification policy routes to
  it with a 30s group wait and a 12h repeat interval.
- Grafana 13.2.4 removed `/api/v1/provisioning/contact-points/test`, and its
  replacement rejects telegram receivers with "unknown integration type", so
  contact-point testing is done by letting the rule fire (or by checking the bot
  credentials with `getMe`/`getChat`), not through that endpoint.
- `"Request Origin is not authorized"` warnings in the Grafana log are the LAN
  entry point (`http://192.168.0.21:3000`) not matching `server.app_url`
  (`http://localhost:3000`); they affect live/websocket features only, set
  `grafana.ini` `server.root_url` if it matters.

### Alert ideas not yet wired up

| Signal | Condition |
| --- | --- |
| `used_percent` on `seaweedfs_disk` | > 80 warn, > 90 critical |
| `used_percent` on `seaweedfs_disk` | > 80 warn, > 90 critical |
| `readonly_volume_count` | > 0 |
| scraper staleness | `now() - max(time) > 60s` per host |
| `deleted_bytes` | > 1 GiB (time to vacuum) |
| `free_bytes` | < 50 GB |
