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
| `volume` | 8080 | stores the actual needle data, one process, two `dataDirs` |
| `filer` | 8888 / 8333 | metadata for the file layout and the S3 gateway |
| `admin` | 23646 | SeaweedFS admin UI/API |
| `worker` | — | background maintenance: `vacuum`, `volume_balance` |

Sizing as deployed (`helm/seaweedfs-values.yaml`):

```
master   1 replica   data 5Gi (local-path)   logs 1Gi
volume   1 replica   data1 50Gi (local-path) maxVolumes: 220
                    misc (hostPath)       maxVolumes: 1500
filer    1 replica   data 5Gi (local-path)   S3 enabled, bucket "lab"
admin    1 replica   data 5Gi (local-path)
worker   2 replicas  jobType: vacuum,volume_balance
```

### What "volume slots" means

This master sets `volumeSizeLimitMB=1000`, so a slot represents roughly 1 GB,
not the 30 GB shown in the binary's version banner. `maxVolumes` is how many
volumes each data directory may hold; when the slots run out the master cannot grow a new volume
**regardless of free disk space**, and any S3 write that needs a new volume fails
with HTTP 500 at the filer (`No writable volumes and no free volumes left`).

That is not theoretical: this lab filled all 35 slots while the disk was only 18 %
used, and the InfluxDB 3 ingester — which keeps its WAL and Parquet files in the
`lab` bucket — stalled completely until slots were added. The 46-slot recovery
also filled up; data1 was raised to 220 slots. Adding getup's `/misc` disk now
provides another 1500 slots, for 1720 total. These are capacity limits, not
preallocated bytes or a change in replication.

`/dir/status` reports both numbers, which is why the cluster dashboard puts them
side by side. Historical outage samples (not current capacity):

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
   IDs; the volume server stores the chunks on `data1` or `misc`.
3. Telegraf reads `master /dir/status` (topology, slots, layouts) and
   `volume /status` (per-disk capacity, volume inventory) and never writes to
   SeaweedFS.
4. Telegraf writes line protocol to the InfluxDB 3 querier, which serves it back
   over InfluxQL to Grafana.

## Failure domains, honestly

| Failure | Impact |
| --- | --- |
| getup unavailable | the bound data1 PV's node affinity prevents scheduling elsewhere; the single volume server stays unavailable |
| /misc absent after reboot | the startup guard refuses to start if misc and root-backed data1 have the same device ID |
| master pod rescheduled | brief metadata outage; data unaffected |
| filer pod rescheduled | S3 metadata unavailable; data unaffected but unreadable through S3 |
| node loss | both of the above, plus manual recovery |

The `local-path` claim is **not** a filesystem quota either: the volume server
stats the underlying host filesystem, so `total_bytes` / `used_bytes` in the
`seaweedfs_disk` metric describe the node's disk, not the 50 GiB claim. Plan
capacity against `free_volume_slots` first and disk bytes second.

See `docs/runbook.md` for the incident where a volume server moved between nodes
and what actually fixed it.

## getup's additional disk

Helm revision 7 registers `/data1` and `/misc` in the same volume-server process,
with `-max 220,1500`. The chart maps host `/misc/object_store/` to container
`/misc/`; it does not replace or migrate the existing data1 PVC.

There is deliberately no pod `nodeSelector`. The existing data1 PV has required
node affinity for `getup`, which already constrains scheduling. On a rebuild,
verify that affinity before starting: these values do not bind a new PVC to getup
by themselves. A second disk on the same node does not add availability.

The `verify-misc-disk` init container compares filesystem device IDs against
root-backed data1. This prevents silently writing to the root filesystem when
the dedicated disk is absent, but is not a disk-identity check and depends on
data1 remaining root-backed. `/misc` was mounted as ext4 from `/dev/sdb1` during
verification; **boot persistence is still unresolved** because `/etc/fstab` had
no entry. The guard does not remount it. See the runbook before rebooting.
