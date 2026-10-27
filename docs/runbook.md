# Runbook

Day-2 operations for this lab. Everything assumes `KUBECONFIG` points at the
cluster and the namespaces from `docs/deployment.md`.

## Capacity

Two limits, check both:

```shell
kubectl -n s3 exec seaweedfs-filer-0 -- \
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
  `-volumeSizeLimitMB=4000` (it was 1000 until 2026-10-26), so each slot is 4 GB. The 1 GB
  era is what caused the outages: 46 slots was a ~46 GB ceiling, and the `lab` collection
  grew 19.5 GB in 2h20m (~8.4 GB/h), so it filled in three hours. At 4 GB the same data needs
  a quarter of the ids, which is why the slot count stopped being the binding limit.
  data1 now has 220 slots and getup's `/misc` disk adds 1500, for 1720 total.
  Slot capacity is not reserved disk space; monitor both filesystems.

  Watch it from the metrics instead of the API: `/dir/status` → `Free` is exported
  as `free_volume_slots` in `seaweedfs_cluster`, and the `SeaweedFS volume slots low`
  Grafana alert fires below 50 — ten hours of warning at the observed ~5 volumes/hour,
  where the original threshold of 5 was about one hour.
- **disk bytes** — per-disk `used_percent` from `seaweedfs_disk`.

To add capacity:

1. more `dataDirs` on the volume server (PVC or hostPath + `maxVolumes`), or
2. another volume server replica (`volume.replicas`, separate `dataDirs`), or
3. larger volumes (`-volumeSizeLimitMB` on the master; raised from 1000 MB to 4000 MB
   on 2026-10-26). The master re-marked every previously-full 1 GB volume as writable within
   a minute (7 → 455 writable), and since SeaweedFS spreads new needles across the whole
   writable pool, no new volume has been created since: at 1 GB the cluster minted ~5
   volumes/hour, at 4 GB those same volumes have ~2.9 GB of headroom each (~1.3 TB of
   absorption) before any new id is needed. The current 4000 MB
   limit makes slot exhaustion outrun disk exhaustion).

Adding replicas also changes the topology tags in `seaweedfs_volume_server`, so
dashboards need no change: they group by `server`.

The full measurement of the two slot-exhaustion incidents, the growth rate and the
The **7-day** retention policy on the `telegraf` database is what bounds growth; the
measurements behind the number are in
[storage-growth-and-retention.md](storage-growth-and-retention.md). Seven days is ~1.15 TB at
the observed 6.8 GB/h, i.e. ~1150 of the 1720 volume slots with roughly 570 free — enough
margin for compaction churn without leaving the slot budget idle. Retention is enforced at
the ingest path as well as by the sweeper: a write older than the cutoff is rejected with
`write timestamp ... is older than the retention period cutoff ...`, so back-filling into a
windowed database fails loudly rather than silently.

The `/misc` data disk is mounted from `/etc/fstab` by UUID
(`UUID=1cb2cc54-… /misc ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2`), and
`findmnt --verify` reports no errors. `nofail` matters: if the disk dies the node still boots
and the volume pod then refuses to start on its guard, which is the failure you want.

## Additional /misc disk on getup

The deployed values preserve the existing data1 PVC (220 slots) and add
`type: hostPath`, `hostPathPrefix: /misc`, `maxVolumes: 1500`. Chart 4.48.0
appends `/object_store/` to that prefix: host `/misc/object_store/` is mounted
at container `/misc/`.

The volume server is pinned to getup with `nodeSelector:
"kubernetes.io/hostname: getup\n"`. That pin is required, not cosmetic: the
hostPath does not follow the pod, and local-path provisions data1 on whatever node
the pod lands on. A clean install without the pin placed the pod on `jump`, bound
data1 there, and the guard aborted with `/misc is not a separate disk`. If you
replace data1's PVC, do it while the pod is pinned — a PVC left bound to another
node leaves the pinned pod unschedulable, which is the intended fail-closed
behaviour rather than a scheduling bug to work around.

**Before rebooting getup:** `/misc` is mounted now and `/etc/fstab` pins it by
UUID with `nofail` (see above). Reboot-persistence of that host mount is a host
concern and was configured outside the SeaweedFS rollout: if it is missing after a
boot, the volume pod refuses to start rather than writing to the root disk. Restore
the mount before restarting the pod; do not format the disk, replace its contents,
or bypass the guard.

The `verify-misc-disk` init container refuses startup when misc and root-backed
data1 have the same filesystem device ID. It does not mount the disk, validate
its UUID, or make storage highly available. If it fails after reboot, restore
the correct host mount before restarting; do not bypass the guard.

Read-only checks (with the correct kubeconfig):

```shell
# On getup: confirm a separate filesystem, not just an existing directory.
findmnt /misc
df -h /misc

kubectl -n s3 get pod seaweedfs-volume-0 -o wide
kubectl -n s3 get pvc data1-seaweedfs-volume-0
# Inspect the bound PV's spec.nodeAffinity using its name from the PVC.
kubectl -n s3 logs seaweedfs-volume-0 -c verify-misc-disk
kubectl -n s3 exec seaweedfs-volume-0 -c seaweedfs -- \
  wget -qO- http://127.0.0.1:8080/status
kubectl -n s3 exec seaweedfs-master-0 -- \
  wget -qO- http://127.0.0.1:9333/dir/status
```

After revision 7, the pod was Ready on getup, `/status` listed both `/data1` and
`/misc`, and the master reported `Max: 1720`. A temporary write/read/delete on
the new disk passed. These were storage/configuration checks, not an authenticated
S3 end-to-end test. Workers run `volume_balance`, so existing volumes may move
onto the new disk; it is not safe to treat it as empty after deployment.

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

Durable prevention: use a PV with node affinity for the node holding the data
(this deployment deliberately has no pod `nodeSelector`), keep volume data on a dedicated
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

## InfluxDB 3 upgrades (db3)

The chart hardcodes the processor's args to `--mode=process,query` and exposes no value
to change them, but this lab's processor node was first created with **ingest** mode,
and InfluxDB 3 refuses to start that node in a mode without it:

```text
Serve command failed: Node with `--node-id db3-influxdb3-enterprise-processor-0` was
previously started with ingest mode; refusing to start without ingest mode
```

So a plain `helm upgrade` leaves the processor in `CrashLoopBackOff`. The release also
needs `--force-conflicts`, because the processor args are owned by `kubectl-edit`.

```shell
helm upgrade db3 influxdata/influxdb3-enterprise -n influxdb3 --version 0.16.1 \
  -f ~/lab/influxdb3-enterprise-values.yaml --force-conflicts    # deliberately no --wait
./helm/patch-db3-processor.sh                                     # restores ingest+webui
kubectl -n influxdb3 delete pod db3-influxdb3-enterprise-processor-0  # only if it was crashlooping
```

A StatefulSet will not replace a crashlooping pod to apply a spec change, so the delete
is what actually makes the patch take effect when the pod is already failing.

Two values in `influxdb3-enterprise-values.yaml` exist for the same reason, and must
stay until the image tag is semver again:

- `acknowledgeCatalogMigration: true` — the chart derives the product version from the
  image tag; `portable-v2` is not semver, so it cannot verify the catalog is v3 and
  fails closed on every upgrade.
- `acknowledgeUpgrade: "3.12"` — same unknown tag for the 3.12 upgrade boundary. The
  chart never records the boundary marker for unknown tags ("even when
  acknowledged"), so this stays permanently rather than being one-time.

`nodeSelector` is intentionally absent from every db3 component: the lab has no
dedicated db3 nodes, and the ingester/compactor hold no local state (the processor's
plugins PVC is the exception, and it is bound to its node by `local-path`). A rebuild
therefore needs no `db3role` node labels.

## Upgrades

- **SeaweedFS**: **always pass `--version`**, and only to a version whose image is
  already in node containerd.

  The repo's `helm/Makefile` exists to make that hard to get wrong:

  ```shell
  make -C helm drift      # is the cluster still what seaweedfs-values.yaml says?
  make -C helm slots      # volume-slot headroom from the master's /dir/status
  make -C helm upgrade    # pinned version, --atomic, --wait, then check `make drift`
  make -C helm rollback   # previous revision
  ```

  `drift` compares the values stored in the release with the file, and the rendered
  manifests with the deployed ones, normalising the two differences Helm always shows
  (hook resources are not stored in `get manifest`; the chart hashes freshly generated
  S3 credentials into a `checksum/` annotation). Anything else is a real drift — a
  `kubectl edit` or a `--set` at upgrade time.

  ```shell
  helm upgrade seaweedfs seaweedfs/seaweedfs -n s3 \
    -f ~/lab/seaweedfs-values.yaml --version 4.48.0 --atomic --wait --timeout 4m
  ```

  Without `--version` Helm pulls the newest chart, which pins an image tag that was
  never published — `chrislusf/seaweedfs:4.49` returns 404 from Docker Hub (the
  registry itself is reachable; `chrislusf/seaweedfs:4.48` and a fresh `busybox:1.36`
  both pull fine) — so all six pods went `ImagePullBackOff` and storage was down for eight minutes
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
