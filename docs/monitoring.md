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

## Alert ideas (not yet wired up)

| Signal | Condition |
| --- | --- |
| `free_volume_slots` | < 5 warn, 0 critical |
| `used_percent` on `seaweedfs_disk` | > 80 warn, > 90 critical |
| `readonly_volume_count` | > 0 |
| scraper staleness | `now() - max(time) > 60s` per host |
| `deleted_bytes` | > 1 GiB (time to vacuum) |
| `free_bytes` | < 50 GB |
