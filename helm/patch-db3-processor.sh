#!/usr/bin/env bash
# Restore this lab's processor flags after a Helm upgrade of db3.
#
# Two reasons this is mandatory, not cosmetic:
#
#   1. InfluxDB 3 refuses to start a node whose identity was first created with ingest
#      mode, in any mode that omits it:
#        "Node with `--node-id db3-influxdb3-enterprise-processor-0` was previously
#         started with ingest mode; refusing to start without ingest mode"
#      The chart hardcodes `--mode=process,query`, so after every upgrade the processor
#      CrashLoopBackOffs until this patch is applied. Losing the web UI is incidental.
#   2. The chart exposes no value for processor args, so this cannot be expressed in
#      values.yaml at all. The drift is deliberate and documented.
#
# StatefulSets will not replace a crashlooping pod to apply a spec change, so after
# patching, delete the pod if it was already crashlooping:
#   kubectl -n influxdb3 delete pod db3-influxdb3-enterprise-processor-0
#
# Full db3 upgrade procedure (see docs/runbook.md):
#   helm upgrade db3 influxdata/influxdb3-enterprise -n influxdb3 --version 0.16.1 \
#     -f ~/lab/influxdb3-enterprise-values.yaml --force-conflicts      # no --wait
#   ~/lab/patch-db3-webui.sh
#   kubectl -n influxdb3 delete pod db3-influxdb3-enterprise-processor-0   # only if needed
#
# --force-conflicts is required because the processor args are owned by kubectl-edit.
#
# Idempotent: exits without touching anything when the args already match.
#
# Usage:
#   ./patch-db3-webui.sh                 # patch + wait for rollout
#   ./patch-db3-webui.sh --check         # report only, exit 1 if it needs patching
set -euo pipefail

NAMESPACE=${NAMESPACE:-influxdb3}
STATEFULSET=${STATEFULSET:-db3-influxdb3-enterprise-processor}
CONTAINER=${CONTAINER:-influxdb3}
KUBECONFIG=${KUBECONFIG:-$HOME/.kube/config}
export KUBECONFIG

# Keep `$(POD_NAME)` and `$(openssl ...)` literal: the container's command wrapper
# expands them at start, which is why they are not evaluated here.
DESIRED_ARGS='["serve","--mode=process,query,ingest,webui","--node-id=$(POD_NAME)","--webui-session-secret=$(openssl rand -base64 24)"]'

current=$(kubectl -n "$NAMESPACE" get statefulset "$STATEFULSET" \
  -o jsonpath="{.spec.template.spec.containers[?(@.name=='$CONTAINER')].args}" 2>/dev/null || true)

if [ -z "$current" ]; then
  echo "error: statefulset $NAMESPACE/$STATEFULSET not found (or has no $CONTAINER container)" >&2
  exit 2
fi

if [ "$current" = "$DESIRED_ARGS" ]; then
  echo "  processor already runs the web UI mode — nothing to do"
  exit 0
fi

echo "  current args: $current"
echo "  desired args: $DESIRED_ARGS"

if [ "${1:-}" = "--check" ]; then
  echo "  web UI patch is MISSING (run without --check to apply)"
  exit 1
fi

patch=$(printf '{"spec":{"template":{"spec":{"containers":[{"name":"%s","args":%s}]}}}}' \
  "$CONTAINER" "$DESIRED_ARGS")
kubectl -n "$NAMESPACE" patch statefulset "$STATEFULSET" --type=strategic -p "$patch"
echo "  patched; waiting for the processor to roll"
kubectl -n "$NAMESPACE" rollout status "statefulset/$STATEFULSET" --timeout=5m
echo "  web UI restored. Reach it with:"
echo "    kubectl -n $NAMESPACE port-forward svc/db3-influxdb3-enterprise-processor 8181:8181"
echo "    then open http://localhost:8181/  (sign in with an admin token)"
