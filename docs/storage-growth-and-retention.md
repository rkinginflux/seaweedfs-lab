# Storage growth and retention

What made the write path fail on 2026-10-23/24, what the numbers actually were, and
what the 8-hour retention policy changed. Everything below is measured, with the
queries and commands needed to reproduce it.

## Summary

- The `lab` S3 bucket grew **19.5 GB in 2 h 20 m (~8.4 GB/h)** and later settled around
  **5–6 GB/h** with no retention in place. That is far more than the Telegraf workload
  itself weighs, so most of it is object-store write amplification (WAL, snapshots,
  compaction output), not raw metric data.
- SeaweedFS volumes in this chart are **1 GB each** (`volumeSizeLimitMB: 1000`), so
  `maxVolumes` is a **GB ceiling**, not a count of large volumes. The cluster hit
  35/35, was raised to 46, and hit **46/46 three hours later** — each time landing on
  zero writable volumes for collection `lab`.
- With no writable volumes the filer returns 500 on PUT, the InfluxDB 3 ingester
  cannot persist WAL or snapshots, `/api/v2/write` starts timing out, and Telegraf
  drops its buffer. **The dashboards go quiet at exactly the moment you need them**,
  because they are fed by the pipeline that just died.
- Fix applied: `maxVolumes` raised to 220 (≈220 GB against ~343 GB free on the volume
  node), and the capacity alert now fires below 5 free slots — it fired at 23:30 with
  4 free, 24 minutes before writes failed.
- The `telegraf` database was given an **8-hour retention policy** on 2026-10-24 03:00 UTC
  and verified live (oldest point exactly 8 h behind). It has since been removed: the lab
  added a second 1.8 TB disk (`misc`) and raised the slot budget to 1720, and the oldest
  point is now ~18 h behind. At the measured ~6 GB/h that is roughly twelve days of
  headroom on 1.7 TB — the window is still the cheap insurance, not a permanent fix.

## Measured growth

Lab collection size, from `seaweedfs_collection.size_bytes`:

| UTC (2026-10-23/24) | lab size | volumes |
| --- | --- | --- |
| 21:20 | 21.38 GB | 27 |
| 22:00 | 28.15 GB | 27 |
| 22:40 | 30.21 GB | 35 |
| 23:00 | 34.23 GB | 35 |
| 23:40 | 40.92 GB | 39 |
| 00:30 | 41.79 GB | 46 (slots exhausted) |
| 01:20 | 43.51 GB | 46 |
| 02:00 | 46.74 GB | 46 |
| 02:40 | 51.19 GB | 55 |
| 03:00 | 52.74 GB | 55 |

21:20 → 23:40 is **+19.5 GB in 140 minutes (8.4 GB/h)**. After the slot fix the rate
eased to roughly **0.7–1.2 GB per 10 minutes (4–7 GB/h)**.

For scale: `cpu` was receiving about **170–190 points per 2 minutes** across five
hosts at the time. The stored data should weigh megabytes per hour, not gigabytes.
The difference is object-store churn — WAL files, snapshot manifests, checkpoints and
compaction run-sets being written and rewritten.

## Why the writes failed

`/dir/status` on the master is the authority:

```shell
kubectl -n s3 exec seaweedfs-master-0 -- \
  wget -qO- http://127.0.0.1:9333/dir/status
```

At 46/46 it reported `"Max": 46, "Free": 0`, and the layouts showed:

```text
collection "lab"      writables: []            <- nothing to write to
collection ""         writables: [4,2,5,6,3,7,1]
```

while the volume server's own `/status` still listed 39 `lab` volumes — all of them
at the 1000 MiB volume limit, therefore not writable. The master could not grow more
because the data node was at `Max`, and growth returned
`automatic volume grow failed: Not enough data nodes found!` — which reads like a
topology problem but is really "no data node has a free slot".

Disk was never the constraint: `/dev/sda2` was 21 % used with 343 GB free throughout.

## Timeline (UTC)

| Time | Event |
| --- | --- |
| 23:23 | 42 volumes, 4 free slots, disk 18 % used |
| 23:30 | `SeaweedFS volume slots low` alert fires (4 < 5) |
| 23:54 | slots hit 0 → filer 500 on PUT → ingester cannot persist WAL → Telegraf writes time out and the buffer overflows (1,336 metrics dropped) |
| 00:31–00:39 | an upgrade attempt without a pinned chart version pulled chart 4.49.0, which pins `chrislusf/seaweedfs:4.49` — a tag that was never published to Docker Hub (404) — so all six SeaweedFS pods went `ImagePullBackOff`; rolled back |
| 00:40 | `maxVolumes` 46 → 220 applied with `--version 4.48.0`; master grew volumes, `lab` had 7 writables, ingestion resumed within the minute (newest sample 00:41:20) |
| ~03:00 | 8 h retention set on the `telegraf` database |

The 23:54–00:40 window is a permanent data gap.

## Verifying the retention policy

Expired data is gone from queries, and the engine is deleting objects rather than
just filtering reads:

```shell
# oldest data still queryable must be ~8h behind now()
SELECT min(time) AS oldest, max(time) AS newest FROM cpu

# the compactor logs every deletion it performs
kubectl -n influxdb3 logs db3-influxdb3-enterprise-compactor-0 --since=30m \
  | grep 'object delete request succeeded'

# the ingester trims WAL behind evicted snapshots
kubectl -n influxdb3 logs db3-influxdb3-enterprise-ingester-0 --since=30m \
  | grep 'WAL retention cleanup'
```

Observed at 03:01 UTC: oldest `cpu` point **2026-10-23 19:01:30** (exactly 8 h),
compactor deleting `checkpoint`, `run_set_index` and `snapshot_manifest` objects every
few seconds, ingester reporting `keep_snapshots=5` WAL cleanup.

## What to expect next

Two different limits, and they move at different speeds:

1. **Logical data** is now capped at 8 h. At the observed 5–6 GB/h write rate expect a
   plateau around **35–45 GB**, down from an unbounded climb. Retention stops the
   bucket growing forever; it does not make it small, because the amplification is in
   the write path, not in how long data is kept.
2. **Physical space** comes back only through SeaweedFS vacuum. At 03:00 the bucket
   reported `size 52.71 GB · logical 52.71 GB` and every volume reported
   `deleted_bytes = 0` — so nothing has been reclaimed yet. The worker vacuums a volume
   once garbage reaches 30 % (`garbage >= 30.00%`), and only then does the disk shrink.

So the expected curve is: growth flattens, `deleted_bytes` starts climbing,
vacuum compacts the fullest volumes, and free disk recovers in steps. If physical size
keeps climbing after logical size has flattened, that is a vacuum problem, not a
retention problem.

## Open questions

- **Write amplification.** ~5 GB/h for ~90 points/minute of metric data deserves an
  explanation that "the WAL writes a lot" does not provide. Worth checking compaction
  frequency, snapshot interval, and whether `pt_wal`/snapshot objects are being deleted
  promptly after compaction (see the `keep_snapshots=5` setting).
- **The bucket is shared.** Four databases live in the same object store —
  `_internal`, `instance_monitoring`, `telegraf`, `usgs`. Only `telegraf` has the 8 h
  policy; `usgs.earthquakes` (17:33 → now) and `instance_monitoring` (18:28 → now)
  were both still accumulating at 03:00. If growth continues after telegraf flattens,
  these are the candidates.
- **Dashboard ranges.** `SeaweedFS — Collections & Growth` defaults to 24 h, so with
  8 h retention two thirds of every panel is legitimately empty. 6 h is the sane
  default now.
- **Slot sizing.** 220 slots is 220 GB of 1 GB volumes. If churn returns, raise
  `volumeSizeLimitMB` rather than `maxVolumes` — 4000 MB volumes would consume a
  quarter of the ids for the same data.

## Reproducing the measurements

```shell
# volume slots, per-collection writables
kubectl -n s3 exec seaweedfs-master-0 -- wget -qO- http://127.0.0.1:9333/dir/status

# physical vs live bytes in the bucket (garbage = size - logical)
kubectl -n s3 exec seaweedfs-master-0 -- \
  sh -c 'echo "s3.bucket.list" | weed shell -master=localhost:9333'

# per-volume size, files, trash and read-only flag (needs detailed_volumes = true)
SELECT volume_id, collection, size_bytes, file_count, deleted_bytes, read_only
FROM seaweedfs_volume WHERE time > now() - interval '5 minutes'
GROUP BY volume_id, collection

# collection growth over time
SELECT date_bin(interval '10 minutes', time) AS t, max(size_bytes)/1e9 AS gb
FROM seaweedfs_collection WHERE collection='lab' AND time > now() - interval '6 hours'
GROUP BY t ORDER BY t
```

## Related

- `docs/runbook.md` — capacity checks, vacuum, the rule that chart upgrades must pin `--version`
- `docs/monitoring.md` — dashboards, including the per-volume inventory, and the capacity alert
- `helm/seaweedfs-values.yaml` — the capacity model in comments, next to `maxVolumes`
