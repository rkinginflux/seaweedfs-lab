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
volume   1 replica   data1 50Gi (local-path) maxVolumes: 35
filer    1 replica   data 5Gi (local-path)   S3 enabled, bucket "lab"
admin    1 replica   data 5Gi (local-path)
worker   2 replicas  jobType: vacuum,volume_balance
```

### What "35 volume slots" means

SeaweedFS pre-allocates volumes of 30 GB each. `maxVolumes: 35` means this volume
server can hold 35 of them, i.e. roughly 1 TB of logical capacity, and — more
importantly — it stops accepting new volumes when the slots run out, regardless of
free disk space.

`/dir/status` reports both numbers, which is why the cluster dashboard puts them
side by side:

```
volume_count      14 → 34   (grows as data arrives)
max_volume_slots  35
free_volume_slots 21 → 1    (the real capacity headroom)
```

At the time of the screenshots the disk was 21 % used (359 GiB free) while only
one volume slot was left. Watching disk bytes alone would have missed the wall
entirely.

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
