# Rebuild checklist

The end-to-end order for standing this lab up from an empty cluster, as actually run.
`docs/deployment.md` covers the pieces in more depth (especially building the Telegraf
image); this document is the sequence, with the traps that cost time called out inline.

Verified against a running lab on 2026-10-24. The original install sequence was
executed; the /misc preflight below documents an additional requirement and
unresolved boot-mount persistence, not a tested empty-cluster rebuild.

Replace the namespace-specific details with your own: the examples use the lab's layout
(`s3`, `influxdb3`, `grafana`, node `eruption`, LAN `192.168.0.21`).

## 0. Preflight

```shell
kubectl config current-context
kubectl get nodes
helm repo add seaweedfs https://seaweedfs.github.io/seaweedfs/helm
helm repo add influxdata https://helm.influxdata.com/
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
```

You need, outside this repo: the license JWT, an InfluxDB 3 admin token, and S3
credentials. Keep them in Secrets, never in values files. **If you are rebuilding onto an
existing object-store catalog, reuse its token and license** — a freshly minted token or
a differently bound license may not authenticate against it, and the SeaweedFS bucket is
existing data, not disposable.

No node labels are required. InfluxDB 3 only selects nodes by label when values say so,
and these values deliberately set no `nodeSelector`.

## 1. SeaweedFS (namespace `s3`)

The values now include getup's dedicated `/misc` disk as a hostPath, in addition
to the existing data1 PVC. Before installing or upgrading:

- Verify the correct filesystem is mounted at `/misc` on getup. The chart uses
  `/misc/object_store/` on the host and `/misc/` in the container.
- The mount is persistent: `/etc/fstab` carries the disk by UUID
  (`UUID=1cb2cc54-… /misc ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2`), and
  `findmnt --verify` reports no errors. `nofail` keeps a failed disk from blocking boot —
  the volume pod's guard then fails closed instead.
- The volume server is now pinned to getup with
  `nodeSelector: "kubernetes.io/hostname: getup\n"` in the values. This is new: a
  hostPath is node-local, and local-path provisions each PVC on whichever node the
  pod first schedules to, so without the pin a fresh install binds data1 anywhere.
  That is not hypothetical — a clean install placed the pod on `jump`, bound data1
  there, and `verify-misc-disk` correctly refused to start
  (`ERROR: /misc is not a separate disk`). With the pin, a new data1 PVC binds on
  getup, which is where the disk is.
- If you ever change the node: move the disk with it, and delete data1's PVC first.
  A PVC left bound to the old node makes the pinned pod unschedulable, not slow.
- Keep the startup guard: it requires misc and root-backed data1 to have distinct
  device IDs. It is not a replacement for the pin or for mount persistence.

See [the /misc runbook](runbook.md#additional-misc-disk-on-getup) for checks and
the observed revision-7 rollout. Do not delete existing PVCs for a rebuild.

```shell
kubectl create namespace s3 --dry-run=client -o yaml | kubectl apply -f -
# create seaweedfs-admin-secret (adminUser, adminPassword) and
# seaweedfs-admin-access-s3 (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)

helm upgrade --install seaweedfs seaweedfs/seaweedfs \
  --version 4.48.0 -n s3 \
  -f helm/seaweedfs-values.yaml \
  --atomic --wait --timeout 10m
```

`--version` is **not** optional. Unpinned, Helm takes the newest chart, and chart 4.49.0
pins `chrislusf/seaweedfs:4.49` — a tag that was never published, so all six pods go
`ImagePullBackOff` and storage is down until you roll back. Check that the image tag
exists, not just the chart version.

```shell
kubectl -n s3 rollout status statefulset/seaweedfs-master --timeout=5m
kubectl -n s3 rollout status statefulset/seaweedfs-volume --timeout=5m
kubectl -n s3 rollout status statefulset/seaweedfs-filer --timeout=5m
kubectl -n s3 get endpointslice -l kubernetes.io/service-name=seaweedfs-s3
```

The S3 endpoint inside the cluster is `http://seaweedfs-s3.s3.svc.cluster.local:8333`, and
the chart's bucket hook should have created the `lab` bucket. Then look at capacity,
because this is the failure that hits first and diagnoses worst:

```shell
kubectl -n s3 exec seaweedfs-master-0 -- wget -qO- http://127.0.0.1:9333/dir/status
```

Expect `Max: 1720` (220 data1 + 1500 misc). `Max` minus volumes in use is the
free-slot count. The pinned chart defaults the master to
`volumeSizeLimitMB` is set to **4000** in the values (it defaulted to 1000 until
2026-10-26), so a slot is **4 GB** — `maxVolumes` is a capacity ceiling, not a count of 30 GB
volumes. At 1000 MB the same data consumed four times the ids, which is what made slots, not
disk, the limit that broke this lab twice.

The budget is per data dir (`data1` 220, `misc` 1500), which is why `Max` is 1720 rather
than 220; the preflight above covers the disk, the PV binding and the startup guard. At
zero free slots the master cannot grow a volume, S3 PUTs return 500, the ingester stalls,
and telemetry stops with no error in Grafana. That happened twice here;
`docs/storage-growth-and-retention.md` has the measurements.

Optional web UIs on the LAN (no auth, lab only):

```shell
kubectl apply -f helm/seaweedfs-ui-proxy.yaml
# volume :8080, master :9333, filer :8888 proxied to :18080, :19333, :18888
```

## 2. InfluxDB 3 Enterprise (namespace `influxdb3`)

Create the four Secrets first — the chart references them by name:

```shell
kubectl create namespace influxdb3 --dry-run=client -o yaml | kubectl apply -f -
# influxdb3-bootstrap-admin-token          admin-token.json
# db3-influxdb3-enterprise-license         license-file
# db3-influxdb3-enterprise-object-storage  access-key-id, secret-access-key
# telegraf-influxdb3-token                 TOKEN   (lab only — scope a real token)

helm upgrade --install db3 influxdata/influxdb3-enterprise \
  --version 0.16.1 -n influxdb3 \
  -f helm/influxdb3-enterprise-values.yaml \
  --wait --timeout 10m
```

One replica each of ingester, compactor, processor and querier. Only the processor's
plugins PVC is node-bound (`local-path`); the ingester and compactor keep everything in
the object store, which is why no component needs a node label.

Two values in that file look odd and are not: `acknowledgeCatalogMigration: true` and
`acknowledgeUpgrade: "3.12"`. The chart derives the product version from the image tag,
the tag here is the custom `portable-v2`, and a non-semver tag makes the chart fail closed
on both the catalog-format check and the 3.12 upgrade boundary. Unknown tags never record
the marker ("even when acknowledged"), so these stay in the file permanently rather than
being one-time flags. Every db3 upgrade also needs `--force-conflicts`, because the
processor args belong to `kubectl-edit` after the patch in 2.1.

If pods were created before the license Secret existed they can retain a directory at
`/etc/influxdb/license` and fail with `Is a directory`; restoring the Secret alone does
not repair them — recreate those pods.

```shell
kubectl -n influxdb3 rollout status statefulset/db3-influxdb3-enterprise-ingester --timeout=5m
kubectl -n influxdb3 rollout status statefulset/db3-influxdb3-enterprise-compactor --timeout=5m
kubectl -n influxdb3 rollout status statefulset/db3-influxdb3-enterprise-processor --timeout=5m
kubectl -n influxdb3 rollout status statefulset/db3-influxdb3-enterprise-querier --timeout=5m
```

### 2.1 Optional processor flags — a one-way door

`helm/patch-db3-modes.sh` adds `--mode=process,query,ingest,webui`, which is what
serves InfluxDB 3 Explorer. On a **fresh** cluster this is optional: the processor starts
fine with the chart's hardcoded `--mode=process,query`, you just get no web UI. Once run:

- InfluxDB 3 pins a node's mode set to its node identity, and afterwards refuses to start
  that node without ingest mode:
  ```text
  Node with `--node-id db3-influxdb3-enterprise-processor-0` was previously started
  with ingest mode; refusing to start without ingest mode
  ```
- The chart exposes no value for processor args, so every `helm upgrade db3` reverts them
  and the processor goes `CrashLoopBackOff` until the patch is re-applied.
- A StatefulSet will not replace a crashlooping pod to apply a spec change, so when the
  pod is already failing the patch only takes effect after deleting it.

The upgrade cycle then becomes:

```shell
helm upgrade db3 influxdata/influxdb3-enterprise -n influxdb3 --version 0.16.1 \
  -f helm/influxdb3-enterprise-values.yaml --force-conflicts   # deliberately no --wait
./helm/patch-db3-modes.sh
kubectl -n influxdb3 delete pod db3-influxdb3-enterprise-processor-0  # only if crashlooping
```

## 3. The database and its retention

Nothing creates this — not the chart, not the secrets bootstrap. Telegraf writes to it, so
create it first, with retention already set:

```shell
TOKEN=$(kubectl -n influxdb3 get secret telegraf-influxdb3-token -o jsonpath='{.data.TOKEN}' | base64 -d)

kubectl -n influxdb3 exec db3-influxdb3-enterprise-querier-0 -- sh -c \
  "influxdb3 create database --retention-period 8h telegraf \
     --token '$TOKEN' --host http://127.0.0.1:8181"
```

Without retention the bucket grew ~6-8 GB/h and exhausted the volume slots twice. The lab
runs **7 days**, which at the measured 6.8 GB/h is ~1.15 TB steady state — about 1150 of the
1720 slots, leaving ~570 free for compaction churn. Size any window the same way: steady
state ≈ rate × window, and it has to fit both the slot budget and the disk that holds it.
`min(time)` is how you check what is enforced, and a write older than the cutoff is rejected
rather than silently dropped. Change it later, and verify:

```shell
kubectl -n influxdb3 exec db3-influxdb3-enterprise-querier-0 -- sh -c \
  "influxdb3 update database -d telegraf -r 8h --token '$TOKEN' --host http://127.0.0.1:8181"

kubectl -n influxdb3 exec db3-influxdb3-enterprise-querier-0 -- sh -c \
  "influxdb3 query --token '$TOKEN' --host http://127.0.0.1:8181 --database telegraf \
     --language sql 'SELECT min(time) AS oldest, max(time) AS newest FROM cpu'"
```

`influxdb3 show databases` lists databases but not retention periods, so query the data.

## 4. Telegraf DaemonSet (namespace `influxdb3`)

```shell
helm upgrade --install telegraf-ds influxdata/telegraf-ds \
  --version 1.1.57 -n influxdb3 \
  -f helm/telegraf-values.yaml \
  --server-side=false --wait --timeout 5m
```

`--server-side=false` avoids a Helm 4 apply conflict with the aggregated
`influx:telegraf` ClusterRole — do not force ownership of its rules. The image is built in
`docs/deployment.md`; it must contain the `inputs.seaweedfs` plugin, and the values point
the DaemonSet at it with `pullPolicy: IfNotPresent`, so the tag has to change on rebuild.

The config sets `detailed_volumes: true`. That is what emits `seaweedfs_volume` (size,
files, trash and read-only per volume) and it is the only reason the per-volume dashboard
has data; without it that dashboard renders empty.

```shell
kubectl -n influxdb3 rollout status daemonset/telegraf-ds --timeout=5m
kubectl -n influxdb3 get pods -l app.kubernetes.io/name=telegraf-ds -o wide
```

Expect one Ready pod per eligible node. A single-replica Deployment is not equivalent for
node-local CPU/disk/network metrics.

## 5. Grafana (namespace `grafana`)

```shell
helm upgrade --install grafana grafana/grafana \
  --version 14.0.1 -n grafana --create-namespace \
  -f helm/grafana-values.yaml \
  --atomic --wait --timeout 10m
```

The chart generates the admin password into the `grafana/grafana` Secret:

```shell
kubectl -n grafana get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

Then provision, in this order, with both port-forwards up:

```shell
kubectl -n grafana port-forward svc/grafana 13000:80 &
kubectl -n influxdb3 port-forward svc/db3-influxdb3-enterprise-querier 18181:8181 &

python3 grafana/build_dashboards.py --verify
INFLUX_URL=http://127.0.0.1:18181 python3 grafana/build_all_measurements_dashboard.py --verify
GRAFANA_URL=http://127.0.0.1:13000 python3 grafana/create_slots_alert.py --test-contact-point
```

- `build_dashboards.py` creates the InfluxQL-mode datasource with the token header, plus
  the Cluster Overview and Collections & Growth dashboards. Run it first — the other two
  resolve the datasource by type and database, and dashboards reference its uid.
- `build_all_measurements_dashboard.py` reads the schema over the querier's HTTP API, so
  it needs `INFLUX_URL`; it builds the dashboard covering every measurement.
- `create_slots_alert.py` provisions the Telegram contact point, the
  `SeaweedFS volume slots low` rule and the notification policy. It reads the bot token
  and chat id from `~/.hermes/.env`. That rule is the only warning you get before writes
  start failing.

```shell
GRAFANA_URL=http://127.0.0.1:13000 python3 - <<'PY'
import base64, json, os, subprocess, urllib.request
g = os.environ["GRAFANA_URL"]
user, pw = subprocess.check_output(
    ["kubectl", "-n", "grafana", "get", "secret", "grafana",
     "-o", "jsonpath={.data.admin-user}{\" \"}{.data.admin-password}"], text=True).split()
auth = base64.b64encode(f"{base64.b64decode(user).decode()}:{base64.b64decode(pw).decode()}".encode()).decode()
req = urllib.request.Request(f"{g}/api/search?query=", headers={"Authorization": "Basic " + auth})
for d in json.load(urllib.request.urlopen(req)):
    print(" ", d["title"], "->", d["url"])
PY
```

## 6. Post-install checks

```shell
# capacity: free slots must be well above zero
kubectl -n s3 exec seaweedfs-master-0 -- wget -qO- http://127.0.0.1:9333/dir/status

kubectl -n s3 exec seaweedfs-master-0 -- wget -qO- http://127.0.0.1:9333/dir/status \
  | ./helm/slots.py

# telemetry freshness: seconds behind the clock, not minutes
kubectl -n influxdb3 exec db3-influxdb3-enterprise-querier-0 -- sh -c \
  "influxdb3 query --token '$TOKEN' --host http://127.0.0.1:8181 --database telegraf \
     --language sql 'SELECT max(time) AS newest FROM cpu WHERE time > now() - interval 5 minute'"

# drift: does the values file still reproduce the cluster?
make -C helm drift

# docs: is the lab's own sequence still in step with this checklist?
make -C helm docs
```

A silent dashboard is almost always one of: the datasource missing its token, Telegraf on
an image without the plugin, the volume servers unreachable from Telegraf, or no writable
volumes left in the `lab` collection.

## Related

- `docs/deployment.md` — building the plugin image, in depth
- `docs/runbook.md` — capacity, vacuum, upgrades, recovery
- `docs/storage-growth-and-retention.md` — why retention is 8 h, with measurements
- `docs/monitoring.md` — dashboards, the alert, and the Grafana/InfluxDB 3 gotchas
