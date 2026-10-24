#!/usr/bin/env bash
# Restore this lab's InfluxDB 3 node modes after a Helm upgrade of db3.
#
# Two components run flags the chart cannot express, and both matter:
#
#   processor  --mode=process,query,ingest,webui
#              InfluxDB 3 pins a node's mode set to its identity. Once this node has
#              started with ingest mode it refuses to start without it:
#                "Node with `--node-id db3-influxdb3-enterprise-processor-0` was
#                 previously started with ingest mode; refusing to start without
#                 ingest mode"
#              The chart hardcodes --mode=process,query, and its wait sees the resulting
#              CrashLoopBackOff. The web UI (InfluxDB 3 Explorer) rides along on this.
#
#   querier    --mode=query,compact
#              The chart hardcodes --mode=query. Compaction is added here deliberately,
#              so if it is no longer wanted, delete the entry below rather than letting
#              an upgrade silently drop it.
#
# StatefulSets will not replace a crashlooping pod to apply a spec change, so when a pod
# is already failing the patch only takes effect after deleting it:
#   kubectl -n influxdb3 delete pod db3-influxdb3-enterprise-processor-0
#
# Full db3 upgrade cycle (see docs/runbook.md):
#   helm upgrade db3 influxdata/influxdb3-enterprise -n influxdb3 --version 0.16.1 \
#     -f helm/influxdb3-enterprise-values.yaml --force-conflicts      # deliberately no --wait
#   ./helm/patch-db3-modes.sh
#   kubectl -n influxdb3 delete pod <the pod that is crashlooping>    # only if needed
#
# --force-conflicts is required because these args are owned by kubectl-edit.
#
# Usage:
#   ./patch-db3-modes.sh            # patch what differs, wait for each rollout
#   ./patch-db3-modes.sh --check    # report only, exit 1 if anything needs patching
set -euo pipefail

NAMESPACE=${NAMESPACE:-influxdb3}
PREFIX=${PREFIX:-db3-influxdb3-enterprise}
CONTAINER=${CONTAINER:-influxdb3}
KUBECONFIG=${KUBECONFIG:-$HOME/.kube/config}
export KUBECONFIG

# component -> desired args. Keep `$(POD_NAME)` and `$(openssl ...)` literal: the
# container's command wrapper expands them at start.
declare -A DESIRED=(
  [processor]='["serve","--mode=process,query,ingest,webui","--node-id=$(POD_NAME)","--webui-session-secret=$(openssl rand -base64 24)"]'
  [querier]='["serve","--mode=query,compact","--node-id=$(POD_NAME)"]'
)

check_only=false
[ "${1:-}" = "--check" ] && check_only=true

status=0
for component in $(printf '%s\n' "${!DESIRED[@]}" | sort); do
  sts="$PREFIX-$component"
  want=${DESIRED[$component]}
  current=$(kubectl -n "$NAMESPACE" get statefulset "$sts" \
    -o jsonpath="{.spec.template.spec.containers[?(@.name=='$CONTAINER')].args}" 2>/dev/null || true)

  if [ -z "$current" ]; then
    echo "  $component: statefulset $NAMESPACE/$sts not found" >&2
    status=1
    continue
  fi
  if [ "$current" = "$want" ]; then
    echo "  $component: already correct"
    continue
  fi

  echo "  $component: needs patching"
  echo "    current: $current"
  echo "    desired: $want"
  if $check_only; then
    status=1
    continue
  fi

  patch=$(printf '{"spec":{"template":{"spec":{"containers":[{"name":"%s","args":%s}]}}}}' \
    "$CONTAINER" "$want")
  kubectl -n "$NAMESPACE" patch statefulset "$sts" --type=strategic -p "$patch" >/dev/null
  kubectl -n "$NAMESPACE" rollout status "statefulset/$sts" --timeout=5m
done

if $check_only && [ $status -ne 0 ]; then
  echo "  result: modes are NOT pinned — run without --check (an upgrade reverted them)"
  exit 1
fi
if $check_only; then
  echo "  result: all node modes match this lab's intent"
fi

echo "  web UI (if the processor was patched):"
echo "    kubectl -n $NAMESPACE port-forward svc/$PREFIX-processor 8181:8181"
echo "    then http://localhost:8181/  — sign in with an admin token"
