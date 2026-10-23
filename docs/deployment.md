# Deployment

Order matters: secrets → SeaweedFS → Telegraf image → Telegraf → Grafana.

## 0. Prerequisites

```shell
helm repo add seaweedfs https://seaweedfs.github.io/seaweedfs/helm
helm repo add influxdata https://helm.influxdata.com/
helm repo update
kubectl create namespace s3
kubectl create namespace influxdb3
kubectl create namespace grafana
```

A storage class named `local-path` is assumed. If yours is different, change
`storageClass` in `helm/seaweedfs-values.yaml`.

## 1. Secrets (never committed)

```shell
NS=s3
# SeaweedFS admin UI
kubectl -n $NS create secret generic seaweedfs-admin-secret \
  --from-literal=adminUser=admin --from-literal=adminPassword="$(openssl rand -base64 24)"
# S3 credentials used by the admin UI
kubectl -n $NS create secret generic seaweedfs-admin-access-s3 \
  --from-literal=AWS_ACCESS_KEY_ID=... --from-literal=AWS_SECRET_ACCESS_KEY=...
# S3 credentials used by the gateway
kubectl -n $NS create secret generic seaweedfs-s3-secret \
  --from-literal=AWS_ACCESS_KEY_ID=... --from-literal=AWS_SECRET_ACCESS_KEY=...
```

The InfluxDB 3 token the Telegraf DaemonSet consumes lives in its own namespace:

```shell
kubectl -n influxdb3 create secret generic telegraf-influxdb3-token \
  --from-literal=TOKEN="$(cat /path/to/token)"
```

## 2. SeaweedFS

```shell
helm install seaweedfs seaweedfs/seaweedfs -n s3 --version 4.48.0 \
  -f helm/seaweedfs-values.yaml
kubectl -n s3 get pods
kubectl -n s3 get pvc
```

Sanity check from inside the cluster:

```shell
kubectl -n s3 exec deploy/seaweedfs-filer -- \
  wget -qO- http://seaweedfs-master.s3.svc.cluster.local:9333/dir/status | head -c 400
```

## 3. Telegraf image with the plugin

Telegraf ships no SeaweedFS input, so the DaemonSet runs a custom image. Base it
on the upstream image and replace only the binary, so the entrypoint, the
`telegraf` user, ca-certificates and nsswitch handling stay identical:

```dockerfile
FROM telegraf:1.40-alpine
COPY telegraf /usr/bin/telegraf
RUN chmod +x /usr/bin/telegraf
RUN telegraf --usage seaweedfs >/dev/null   # build-time smoke test
```

Build the binary with a version stamp (otherwise `telegraf --version` says
"unknown"), copy `telegraf-plugin/` into a Telegraf checkout as
`plugins/inputs/seaweedfs`, add the registration file, then:

```shell
cd /path/to/telegraf
cat > plugins/inputs/all/seaweedfs.go <<'EOF'
//go:build !custom || inputs || inputs.seaweedfs

package all

import _ "github.com/influxdata/telegraf/plugins/inputs/seaweedfs" // register plugin
EOF

CGO_ENABLED=0 go build -trimpath \
  -ldflags "-w -s \
    -X github.com/influxdata/telegraf/internal.Version=1.40.7-seaweedfs \
    -X github.com/influxdata/telegraf/internal.Commit=$(git rev-parse --short=8 HEAD) \
    -X github.com/influxdata/telegraf/internal.Branch=seaweedfs" \
  -o /path/to/image-build/telegraf ./cmd/telegraf

cd /path/to/image-build
docker build --provenance=false --sbom=false --platform linux/amd64 \
  -t <registry>/telegraf:1.40.7-seaweedfs .
docker run --rm <registry>/telegraf:1.40.7-seaweedfs --version
docker push <registry>/telegraf:1.40.7-seaweedfs
```

`--provenance=false --sbom=false` avoids attestation manifests that can confuse
`ctr images import` if you load images into nodes directly instead of pushing.

If the nodes have no registry access, import the tarball into each node's
containerd store with a privileged, `hostPID` pod that enters the host
namespaces:

```shell
kubectl -n default run imgload --image=busybox:1.36 --restart=Never \
  --overrides='{"spec":{"nodeName":"<node>","hostPID":true,"containers":[{"name":"loader","image":"busybox:1.36","command":["sleep","1800"],"securityContext":{"privileged":true},"volumeMounts":[{"name":"host-tmp","mountPath":"/host-tmp"}]}],"volumes":[{"name":"host-tmp","hostPath":{"path":"/var/tmp","type":"Directory"}}]}}'
kubectl cp telegraf-1.40.7-seaweedfs.tar default/imgload:/host-tmp/
kubectl exec imgload -- nsenter -t 1 -m -u -i -n -- \
  ctr -n k8s.io images import /var/tmp/telegraf-1.40.7-seaweedfs.tar
kubectl delete pod imgload
```

## 4. Telegraf DaemonSet

```shell
helm install telegraf-ds influxdata/telegraf-ds -n influxdb3 \
  -f helm/telegraf-values.yaml
kubectl -n influxdb3 get ds telegraf-ds
kubectl -n influxdb3 logs ds/telegraf-ds | grep "Loaded inputs"
```

Expected: `Loaded inputs: cpu disk diskio internal kernel kube_inventory mem net
processes prometheus seaweedfs statsd swap system`.

Rolling out a plugin change later:

```shell
# build + push a new tag, then
helm upgrade telegraf-ds influxdata/telegraf-ds -n influxdb3 \
  -f helm/telegraf-values.yaml --atomic --wait
```

With `pullPolicy: IfNotPresent`, re-pushing the *same* tag is never picked up —
bump the tag.

## 5. Grafana

See `docs/monitoring.md`. Either import `grafana/*.json` by hand, or run
`grafana/build_dashboards.py`, which creates the datasource and both dashboards
from the same definitions.

## Verification checklist

```shell
kubectl -n influxdb3 logs ds/telegraf-ds | grep -i "E!"        # no errors
kubectl -n influxdb3 exec deploy/telegraf-ds -- telegraf --usage seaweedfs
# then, against InfluxDB 3:
#   SELECT host, max(time) FROM seaweedfs_cluster GROUP BY host
```

Every node should report the same cluster from its own point of view (that is
expected: a DaemonSet scrapes once per node).
