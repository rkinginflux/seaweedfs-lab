# Weekend report — 23–26 October 2026

Three days of telemetry on the lab's SeaweedFS tier: **+434 GB written, 455 volumes, 912k
chunks, no data loss** — and two capacity incidents that both ended with the same lesson
(volume *slots*, not disk space, is the limit that bites here).

Telemetry window: 2026-10-23 23:00 → 2026-10-26 13:20 UTC (62 h). Numbers below are from the
master's `/dir/status`, the volume server's `/status`, and the `telegraf` database.

## At a glance

| | value |
| --- | --- |
| Bucket `lab` | **464 GB** (911,933 chunks, 911,938 files) |
| Written this weekend | **+434 GB**, ≈ **7.0 GB/h** sustained |
| Volumes allocated | 455 of 1720 slots (**1265 free**) |
| Disk `/data1` (PVC, 220-slot budget) | 315 GB used · 175 GB free — **at its slot ceiling** |
| Disk `/misc` (hostPath, 1500-slot budget) | 340 GB used · **1.6 TB free** |
| Collections | `lab` (SeaweedFS objects) + `default` (filer metadata), 7 writable volumes each |
| Read-only volumes | 0 |
| Highest volume ID | 518 (455 live ⇒ 63 reclaimed by compaction/vacuum) |
| Ingestion coverage | 369 of 372 ten-minute bins — 30 min lost across the incidents |
| Compaction backlog (peak) | 9 snapshots · lag peak 116 s |

## Cluster overview

![SeaweedFS cluster overview over the weekend](screenshots/03-weekend-cluster-overview.png)

The shape of the weekend is the middle chart: `/misc` climbs from zero when the second disk
came online, while `/data1` plateaus once it hits its own 220-slot budget. Both are healthy;
what matters is the **Volume slots free** stat at top right — 1265 — because at zero the
filer returns `500 No writable volumes and no free volumes left` no matter how much disk is
left.

## Growth

![Collections and growth over the weekend](screenshots/04-weekend-collections-growth.png)

Growth is dead linear at ~7 GB/h, which is Telegraf + InfluxDB 3 Enterprise WAL/Parquet for
five nodes, with no retention window in place. `lab` went from 30 GB to 464 GB in 62 hours.
Total files crossed 900k, which is why compaction and vacuum matter as much as capacity:
63 volumes have already been reclaimed behind the current 455.

## Per-volume view

![Per-volume inventory and fill](screenshots/05-weekend-volumes.png)

Volumes fill to ~96% before the master hands out a new one (the sawtooth), and the fullest
is 1003.9 MiB against the 1000 MB limit — normal overshoot from concurrent appends. The
inventory table is where a single hot volume shows up.

## What broke, and what it cost

| time (UTC) | event |
| --- | --- |
| 23 Oct 18:30 | unpinned `helm upgrade` took chart 4.49.0, which pins `chrislusf/seaweedfs:4.49` — a tag that was never published (404). All six pods `ImagePullBackOff`, storage down. Rolled back to 4.48.0. |
| 23 Oct 19:33 | 35/35 slots exhausted by an earlier configuration: S3 PUTs returned 500 and the InfluxDB 3 ingester stalled. |
| 23 Oct 20:40 | `maxVolumes` 220 applied with the chart pinned; master grew volumes, ingestion resumed. |
| 23 Oct 23:30 | 46/46 again — the 220 value was on the wrong values path, so the release came back at 46. Alert fired at 4 free slots. |
| 24 Oct 00:41 | fixed properly; ingestion resumed within a minute of the rollout. |
| 24 Oct 16:40 | second data dir on a 1.8 TB disk (`/misc`), 1500 more slots, plus an initContainer that refuses to start if `/misc` is not a separate device. |
| 25–26 Oct | steady: ~7 GB/h, no alert, no restarts of any db3 or SeaweedFS pod. |

Both capacity incidents cost about 30 minutes of telemetry in total, and both were
misdiagnosed at first as disk problems — the disk was under 25% used each time. The volume
server runs `volumeSizeLimitMB: 1000`, so a slot is 1 GB, not the 30 GB the `weed` version
banner advertises; `maxVolumes` is therefore a **GB ceiling**, not a count of large volumes.

## Telemetry quality

- Ingest gaps: 369/372 ten-minute bins over the weekend. The missing three line up with the
  rollouts above, not with load.
- Telegraf: 0 ongoing write errors; the counter recorded 63 during the outages only.
- Compaction: backlog peaked at 9 uncompacted snapshots with 116 s of lag, then drained.
  Deferred snapshots stayed at 0 — the gate that stalls `--recover-deferred-snapshots` never
  engaged.
- No pod restarts in `s3` or `influxdb3` after Friday's work; the db3 processor shows 2
  restarts, both from the deliberate mode patches.

## Headroom — and what was done about it on the Monday

At 6.8 GB/h the 1265 free slots were about **7–8 days** of runway, and `/misc` had 1.6 TB
free, so the slot budget would run out before the disk — the same asymmetry that caused both
outages.

Both open items are now closed:

1. **Retention is back on: 7 days** on the `telegraf` database (26 Oct). Sized from the
   measured rate, not by feel: 6.8 GB/h × 168 h ≈ **1.15 TB steady state**, which is ~1150 of
   the 1720 slots with ~570 free for compaction churn, and fits comfortably in the 1.6 TB
   free on `/misc`. Enforcement was verified on a throwaway database with a 1 h window — the
   ingester rejected a 3 h-old write with `write timestamp ... is older than the retention
   period cutoff ...`, which is stronger evidence than waiting for the sweeper.
2. **`/misc` is persistent.** `/etc/fstab` pins it by UUID
   (`UUID=1cb2cc54-… /misc ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2`),
   `misc.mount` is active, and `findmnt --verify` reports no errors. `nofail` is deliberate:
   a dead disk should not block the node's boot — the volume pod's initContainer then fails
   closed instead.

Still worth doing at the next quiet moment: raise `volumeSizeLimitMB` (e.g. 4000) so the same
data uses a quarter of the volume ids, which cuts master and vacuum churn for free.

---

Screenshots rendered server-side by the in-cluster Grafana image renderer (`/render/...`),
added to the Grafana release this weekend because the headless browser cannot keep a
dashboard tab visible long enough for the panels to paint.

Dashboards: `SeaweedFS — Cluster Overview`, `SeaweedFS — Collections & Growth`,
`SeaweedFS — Volumes`, all regenerable from `grafana/build_dashboards.py`.
