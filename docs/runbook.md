# Runbook

Day-2 operations for this lab. Everything assumes `KUBECONFIG` points at the
cluster and the namespaces from `docs/deployment.md`.

## Capacity

Two limits, check both:

```shell
kubectl -n s3 exec deploy/seaweedfs-filer -- \
  wget -qO- http://seaweedfs-master.s3.svc.cluster.local:9333/dir/status
```

- **volume slots** — `Max` − used from `/dir/status`. At 0 the master cannot grow a
  new volume and writes fail, even with free disk. This is the limit that bites
  this lab: it hit 35/35, then 46/46, both times with the disk under 25 % used.
  Every S3 PUT starts returning 500 (`No writable volumes and no free volumes
  left`), which stalls the InfluxDB 3 ingester and takes ingestion down with it —
  the failure is silent on the dashboards past that point, because the dashboards
  are fed by the pipeline that just died.

  **Size the limit against volume size, not disk size.** This cluster runs
  `-volumeSizeLimitMB=1000`, so each slot is 1 GB, not the 30 GB the binary's
  version string advertises. 46 slots was therefore only a ~46 GB ceiling, and the
  `lab` collection grew 19.5 GB in 2h20m (~8.4 GB/h), so it filled in three hours.
  The limit is now 220 (≈220 GB, against 343 GB free on `/dev/sda2`).

  Watch it from the metrics instead of the API: `/dir/status` → `Free` is exported
  as `free_volume_slots` in `seaweedfs_cluster`, and the `SeaweedFS volume slots
  low` Grafana alert fires below 5.
- **disk bytes** — per-disk `used_percent` from `seaweedfs_disk`.

To add capacity:

1. more `dataDirs` on the volume server (new PVC + `maxVolumes`), or
2. another volume server replica (`volume.replicas`, separate `dataDirs`), or
3. larger volumes (`-volumeSizeLimitMB` on the volume server; the default 30 GB
   chunk size is what makes slot exhaustion outrun disk exhaustion).

Adding replicas also changes the topology tags in `seaweedfs_volume_server`, so
dashboards need no change: they group by `server`.

The full measurement of the two slot-exhaustion incidents, the growth rate and the
8 h retention policy is in [storage-growth-and-retention.md](storage-growth-and-retention.md).

## Vacuum

`seaweedfs_collection.deleted_bytes` above zero means deleted needles still occupy
disk. The chart runs two `worker` pods with `jobType: vacuum,volume_balance`, so
this usually resolves itself; to do it by hand:

```shell
kubectl -n s3 exec -it deploy/seaweedfs-master -- weed shell
> volume.list
> volume.vacuum -garbageThreshold=0.3
> exit
```

Do it in a quiet window: vacuum rewrites volume files and briefly doubles IO.

## A volume server moved nodes and S3 started returning 500

Symptom: `GET` of an object through S3 returns 500 while the filer is healthy; the
filer log says `volume <id> not found`; the master `/dir/status` no longer lists
the volume IDs that the filer metadata still references.

Cause: with `local-path` (and previously `hostPath`) the volume data is pinned to
the node the pod was scheduled on. If the pod is recreated on another node it
starts with an empty directory, registers itself, and the volumes that only exist
on the old node are gone from the master's view.

What actually fixed it, in order:

1. **Stop.** Do not delete the bucket, the catalog prefix or the PVC to make the
   error go away — the metadata points at chunks that still exist on another node.
2. Inventory the nodes. Read-only debug pods with a hostPath mount of the node's
   local-path directory, `ls` the `*.dat` / `*.idx` / `*.vif` files, and compare
   volume IDs with what the master lists. Volume IDs overlap across nodes; never
   merge directories blindly.
3. Start a **separate, node-pinned volume server** that announces the old
   directory to the master under its own identity (distinct pod IP, no overlapping
   volume IDs). Keep the current server running so both volume sets are
   registered at once.
4. Verify: authenticated `HEAD` and `GET` of the affected object through the S3
   endpoint, and no `volume not found` in a fresh filer log window.
5. Only then restart the dependent workloads, and afterwards run a `weed` fsck /
   audit for dangling metadata.

Durable prevention: pin the volume server to a node whose disk holds the data
(`nodeSelector` or a PV with node affinity), keep volume data on a dedicated
volume, and back it up. A single-replica volume server on node-local storage is a
single-node failure domain — this lab now has snapshots of each node's volume
directory for exactly this reason.

## Backups

What is worth copying, in order of pain when lost:

1. volume data directories on each node (`*.dat`, `*.idx`, `*.vif`)
2. filer metadata (its `local-path` PVC) — without it the chunk IDs are unreadable
3. master metadata (volume slot bookkeeping; rebuildable, but it tells you which
   volume IDs were ever assigned)

A live `tar` of a volume directory is a best-effort copy, not an atomic snapshot;
verify the checksum after copying and re-run `weed` fsck after a restore.

## Checking the pipeline

```shell
# Telegraf
kubectl -n influxdb3 logs ds/telegraf-ds --tail=50 | grep -Ei "E!|seaweedfs"
kubectl -n influxdb3 exec deploy/telegraf-ds -- telegraf --usage seaweedfs

# data landing (InfluxQL through the querier)
kubectl -n influxdb3 port-forward svc/influxdb3-querier 8181:8181 &
curl -s -X POST http://127.0.0.1:8181/query?db=telegraf \
  -H "Authorization: Token $TOKEN" \
  --data-urlencode 'q=SELECT count("volume_count") FROM "seaweedfs_cluster"'
```

A silent dashboard is almost always one of: token missing on the datasource, the
Telegraf pod not on the plugin image, or the volume servers unreachable from
Telegraf (discovery) — in that order.

## Upgrades

- **SeaweedFS**: **always pass `--version`**, and only to a version whose image is
  already in node containerd.

  ```shell
  helm upgrade seaweedfs seaweedfs/seaweedfs -n s3 \
    -f ~/lab/seaweedfs-values.yaml --version 4.48.0 --atomic --wait --timeout 4m
  ```

  Without `--version` Helm pulls the newest chart, which referenced
  `chrislusf/seaweedfs:4.49` — an image the nodes cannot pull (no registry access),
  so all six pods went `ImagePullBackOff` and storage was down for eight minutes
  until `helm rollback seaweedfs <rev>` restored the previous revision. `--atomic`
  does not save you here: it only rolls back when *Helm* times out, and a stuck
  `pending-upgrade` blocks the next upgrade until it is resolved.

  Before any upgrade, confirm the image is present: `ctr -n k8s.io images ls` on
  each node, or import it with `load-image.sh` the way the Telegraf image is
  handled. Volume format changes still need a maintenance window and a backup.
- **Telegraf**: rebuild the image with a new tag, `helm upgrade --atomic --wait`;
  `pullPolicy: IfNotPresent` means the tag must change.
- **Grafana**: dashboards live in Grafana's database. The JSON exports in
  `grafana/` are the source of truth; re-run `build_dashboards.py` after a
  reinstall.
