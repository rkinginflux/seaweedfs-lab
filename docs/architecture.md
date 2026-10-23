# Architecture

## Kubernetes cluster

Five nodes, kubeadm, no cloud: one control plane (`eruption`) and four workers
(`getup`, `jump`, `panama`, `topjimmy`). There is no CSI with real quotas —
persistence is `rancher.io/local-path`, so every bound volume lives on the node
its scheduler picked, with node affinity and reclaim policy `Delete`.

That single design decision drives most of the operational notes in this repo:
`local-path` gives node-local durability, not availability.

## SeaweedFS components

| Component | Port | Role in this lab |
| --- | --- | --- |
| `master` | 9333 | cluster metadata: topology, volume assignment, volume slots |
| `volume` | 8080 | stores the actual needle data on disk, one process, one `dataDir` |
| `filer` | 8888 / 8333 | metadata for the file layout and the S3 gateway |
| `admin` | 23646 | SeaweedFS admin UI/API |
| `worker` | — | background maintenance: `vacuum`, `volume_balance` |

Sizing as deployed (`helm/seaweedfs-values.yaml`):

```
master   1 replica   data 5Gi (local-path)   logs 1Gi
volume   1 replica   data1 50Gi (local-path) maxVolumes: 46
filer    1 replica   data 5Gi (local-path)   S3 enabled, bucket "lab"
admin    1 replica   data 5Gi (local-path)
worker   2 replicas  jobType: vacuum,volume_balance
```

### What "volume slots" means

SeaweedFS allocates volumes in 30 GB chunks. `maxVolumes` is how many of them this
volume server may hold; when the slots run out the master cannot grow a new volume
**regardless of free disk space**, and any S3 write that needs a new volume fails
with HTTP 500 at the filer (`No writable volumes and no free volumes left`).

That is not theoretical: this lab filled all 35 slots while the disk was only 18 %
used, and the InfluxDB 3 ingester — which keeps its WAL and Parquet files in the
`lab` bucket — stalled completely until slots were added. `maxVolumes` is now 46
(35 existing volumes + 11 more, the number that fits in the ~354 GB free at 30 GB
per volume).

`/dir/status` reports both numbers, which is why the cluster dashboard puts them
side by side:

```
volume_count      14 → 42   (grows as data arrives; 35 at slot exhaustion)
max_volume_slots  46        (was 35)
free_volume_slots 21 → 0 → 4 (the real capacity headroom)
```

At the time of the screenshots the disk was 21 % used (359 GiB free) while a single
volume slot was left. Watching disk bytes alone would have missed the wall
entirely — and it did: the slots hit 0 an hour later.

### Collections and replication

The deployment uses replication `000` — a single copy of every needle. Two
layouts exist, the default collection (no name) and `lab`, which is the bucket
InfluxDB 3 writes to. Because replication is `000`, the "volume count" of a
collection equals its logical volume count; with replication `001` or higher the
number is a multiple, which is why the dashboard says "physical volumes".

## Data path

1. InfluxDB 3 Enterprise writes Parquet files to the `lab` bucket through the S3
   gateway on the filer.
2. The filer stores object metadata (its own local-path PVC) and hands out chunk
   IDs; the volume server stores the chunks on `data1`.
3. Telegraf reads `master /dir/status` (topology, slots, layouts) and
   `volume /status` (per-disk capacity, volume inventory) and never writes to
   SeaweedFS.
4. Telegraf writes line protocol to the InfluxDB 3 querier, which serves it back
   over InfluxQL to Grafana.

## Failure domains, honestly

| Failure | Impact |
| --- | --- |
| volume pod rescheduled to another node | data is on `local-path`, so the volume server starts empty and the master unregisters the missing volumes → S3 reads of those chunks fail with 500 |
| master pod rescheduled | brief metadata outage; data unaffected |
| filer pod rescheduled | S3 metadata unavailable; data unaffected but unreadable through S3 |
| node loss | both of the above, plus manual recovery |

The `local-path` claim is **not** a filesystem quota either: the volume server
stats the underlying host filesystem, so `total_bytes` / `used_bytes` in the
`seaweedfs_disk` metric describe the node's disk, not the 50 GiB claim. Plan
capacity against `free_volume_slots` first and disk bytes second.

See `docs/runbook.md` for the incident where a volume server moved between nodes
and what actually fixed it.
