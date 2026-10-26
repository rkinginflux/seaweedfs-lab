#!/usr/bin/env python3
"""Build a styled Grafana dashboard covering EVERY measurement in the
InfluxDB 3 `telegraf` database.

Design:
  * summary row of stat panels for the signals you look at first
  * one row per operational area (host, seaweedfs, telegraf, kubernetes, influxdb3 ...)
  * measurements that belong together are merged into one panel (one target each)
    so the dashboard stays readable and the browser does not fire 130 queries
  * every measurement in the database appears in exactly one target — asserted
  * units, thresholds and legends are derived from the field types / names

Usage:
  kubectl -n grafana port-forward svc/grafana 3000:80 &
  kubectl -n influxdb3 port-forward svc/influxdb3-querier 8181:8181 &
  python3 build_all_measurements_dashboard.py [--verify] [--dry-run]

Environment: GRAFANA_URL, INFLUX_URL, DS_UID, KUBECONFIG, GRAFANA_USER/GRAFANA_PASS
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict

GRAFANA = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
INFLUX = os.environ.get("INFLUX_URL", "http://127.0.0.1:8181")
KUBECONFIG = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
DASH_UID = "telegraf-all-measurements"
DASH_TITLE = "Telegraf — All Measurements"
GROUP_NS = "influxdb3"
TOKEN_NS = "influxdb3"
TOKEN_SECRET = "telegraf-influxdb3-token"


# --------------------------------------------------------------- credentials
def kubectl_secret(namespace: str, name: str, key: str) -> str:
    raw = subprocess.check_output(
        ["kubectl", "-n", namespace, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        env={**os.environ, "KUBECONFIG": KUBECONFIG})
    return base64.b64decode(raw).decode().strip()


USER = os.environ.get("GRAFANA_USER") or kubectl_secret("grafana", "grafana", "admin-user")
PASS = os.environ.get("GRAFANA_PASS") or kubectl_secret("grafana", "grafana", "admin-password")
TOKEN = kubectl_secret(TOKEN_NS, TOKEN_SECRET, "TOKEN")
AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()


def grafana(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(GRAFANA + path, data=data, method=method,
                                 headers={"Authorization": "Basic " + AUTH,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def datasource_uid() -> str:
    uid = os.environ.get("DS_UID")
    if uid:
        return uid
    _, dss = grafana("GET", "/api/datasources")
    for ds in dss or []:
        if ds["type"] == "influxdb" and (ds.get("jsonData") or {}).get("dbName") == "telegraf":
            return ds["uid"]
    sys.exit("no InfluxDB datasource for the telegraf database found in Grafana")


# ------------------------------------------------------------------- schema
def influx_query(sql: str):
    body = json.dumps({"db": "telegraf", "q": sql, "format": json if False else "json"}).encode()
    req = urllib.request.Request(INFLUX + "/api/v3/query_sql", data=body,
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"InfluxDB query failed: {e.code} {e.read().decode()[:300]}")


def load_schema():
    rows = influx_query("SELECT table_name, column_name, data_type FROM "
                        "information_schema.columns WHERE table_schema='iox' "
                        "ORDER BY table_name, ordinal_position")
    columns: dict[str, dict[str, str]] = defaultdict(dict)
    for r in rows:
        columns[r["table_name"]][r["column_name"]] = r["data_type"]
    tags = {m: [c for c, t in cols.items() if c != "time" and "Dictionary" in t]
            for m, cols in columns.items()}
    fields = {m: {c: t for c, t in cols.items()
                  if c != "time" and "Dictionary" not in t}
              for m, cols in columns.items()}
    return dict(columns), tags, fields


def qname(name: str) -> str:
    return '"' + name.replace('"', '\\"') + '"'


# ------------------------------------------------------- measurement classes
# unit inference from the metric name
def unit_for(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("_bytes", "bytes_", "mem_pool", "memstats")):
        return "bytes"
    if "percent" in n:
        return "percent"
    if n.endswith("_seconds") or "_seconds_" in n or "duration" in n or "latency" in n \
            or "poll_time" in n or "ttfb" in n or "wait_duration" in n:
        return "s"
    if "requests" in n:
        return "reqps"
    if "rows" in n or "lines" in n or "objects" in n or "files" in n \
            or "partitions" in n or "tables" in n:
        return "short"
    return "ops"


def rate_expr(field: str, alias: str) -> str:
    return f'non_negative_derivative(max("{field}"), 1s) AS "{alias}"'


def classify(name: str, fields: dict[str, str]) -> tuple[str, str, str, str]:
    """(group, unit, expression list key, aggregation hint) for one measurement."""
    n = name
    # explicit families -----------------------------------------------------
    if n == "cpu":
        return ("Host / OS", "percent", "cpu", "")
    if n == "mem":
        return ("Host / OS", "percent", "mem", "")
    if n == "swap":
        return ("Host / OS", "percent", "swap", "")
    if n == "disk":
        return ("Host / OS", "percent", "disk", "")
    if n == "diskio":
        return ("Host / OS", "ops", "diskio", "")
    if n == "net":
        return ("Host / OS", "bps", "net", "")
    if n == "kernel":
        return ("Host / OS", "ops", "kernel", "")
    if n == "processes":
        return ("Host / OS", "short", "processes", "")
    if n == "system":
        return ("Host / OS", "short", "system", "")
    if n.startswith("seaweedfs_"):
        return ("SeaweedFS", "auto", n, "")
    if n.startswith("internal_"):
        return ("Telegraf", "auto", n, "")
    if n.startswith("kubernetes_"):
        return ("Kubernetes", "auto", n, "")
    if n == "process_start_time_seconds":
        return ("Memory & process", "dateTimeAsIso", n, "")
    if n == "datafusion_mem_pool_bytes" or n == "jemalloc_memstats_bytes":
        return ("Memory & process", "bytes", n, "")
    if n.startswith("thread_panic"):
        return ("Tokio & failures", "short", n, "")
    if n.startswith("query_datafusion_query_execution_ooms"):
        return ("Query path", "short", n, "")
    if n.startswith("http_"):
        return ("Query path", "auto", n, "")
    if n.startswith("influxdb_iox_query_log_"):
        return ("Query path", "auto", n, "")
    if n.startswith("iox_async_semaphore_"):
        return ("Async semaphore", "auto", n, "")
    if n.startswith("object_store_"):
        return ("Object store", "auto", n, "")
    if n.startswith("tokio_"):
        return ("Tokio & failures", "auto", n, "")
    if n.startswith("influxdb3_wal_") or n.startswith("influxdb3_snapshot_"):
        return ("InfluxDB 3 — WAL & snapshots", "auto", n, "")
    if n.startswith("influxdb3_"):
        return ("InfluxDB 3 — ingest & writes", "auto", n, "")
    return ("Other", "auto", n, "")


# ------------------------------------------------------------- expressions
HOST_QUERIES = {
    # key: (expressions, group-by tags, description)
    "cpu": (['100 - mean("usage_idle") AS "busy"'], ["host"], "CPU busy, from idle time"),
    "mem": (['mean("used_percent") AS "used"'], ["host"], "Memory utilisation"),
    "swap": (['mean("used_percent") AS "used"'], ["host"], "Swap utilisation"),
    "disk": (['max("used_percent") AS "used"',
              'max("inodes_used_percent") AS "inodes"'], ["host", "path"],
             "Space and inode usage per filesystem"),
    "diskio": (['non_negative_derivative(max("reads"), 1s) AS "reads"',
                'non_negative_derivative(max("writes"), 1s) AS "writes"'], ["host"],
               "Read/write operations per second, summed across devices"),
    "net": (['non_negative_derivative(max("bytes_recv"), 1s) * 8 AS "rx"',
             'non_negative_derivative(max("bytes_sent"), 1s) * 8 AS "tx"'], ["host"],
            "Throughput in bits/s, summed across interfaces"),
    "kernel": (['non_negative_derivative(max("context_switches"), 1s) AS "ctx_switches"',
                'non_negative_derivative(max("interrupts"), 1s) AS "interrupts"',
                'non_negative_derivative(max("processes_forked"), 1s) AS "forks"'], ["host"],
               "Kernel event rates"),
    "processes": (['mean("running") AS "running"', 'mean("blocked") AS "blocked"',
                   'mean("zombies") AS "zombies"', 'mean("total") AS "total"'], ["host"],
                  "Process counts"),
    "system": (['mean("load1") AS "load1"', 'mean("load5") AS "load5"',
                'mean("load15") AS "load15"', 'mean("uptime") AS "uptime"'], ["host"],
               "Load averages and uptime"),
}

SEAWEEDFS_QUERIES = {
    "seaweedfs_cluster": (['max("volume_count") AS "volumes"',
                           'max("max_volume_slots") AS "slots"',
                           'max("free_volume_slots") AS "free_slots"',
                           'max("volume_server_count") AS "servers"'], [], "Cluster volumes and slots"),
    "seaweedfs_disk": (['max("used_percent") AS "used %"', 'max("free_bytes") AS "free"'],
                       ["path"], "Volume-server disk usage"),
    "seaweedfs_collection": (['max("size_bytes") AS "size"', 'max("file_count") AS "files"'],
                             ["collection"], "Per-collection size and files"),
    "seaweedfs_volume_server": (['max("volume_count") AS "volumes"',
                                 'max("free_volume_slots") AS "free_slots"'], [], "Per-server inventory"),
    "seaweedfs_master": (['max("max_volume_id") AS "max_volume_id"'], [], "Highest assigned volume ID"),
}

INTERNAL_QUERIES = {
    "internal_agent": (['non_negative_derivative(max("metrics_gathered"), 1s) AS "gathered"',
                        'non_negative_derivative(max("metrics_written"), 1s) AS "written"',
                        'non_negative_derivative(max("metrics_dropped"), 1s) AS "dropped"',
                        'non_negative_derivative(max("write_errors"), 1s) AS "write_errors"'], [],
                       "Metric throughput and drops"),
    "internal_gather": (['mean("gather_time_ns") AS "gather_ns"'], ["input"], "Gather duration per input"),
    "internal_memstats": (['mean("heap_in_use_bytes") AS "heap_in_use"',
                           'mean("heap_sys_bytes") AS "heap_sys"',
                           'mean("alloc_bytes") AS "alloc"'], [], "Telegraf heap usage"),
    "internal_process": (['mean("cpu") AS "cpu"', 'mean("resident_memory") AS "rss"'], [],
                         "Telegraf process CPU and RSS"),
    "internal_prometheus": (['max("connection_status") AS "connected"'], ["input"], "Scrape connection state"),
    "internal_statsd": (['max("pending_messages") AS "pending"'], [], "StatsD queue depth"),
    "internal_write": (['100 * max("buffer_size") / max("buffer_limit") AS "buffer %"',
                        'non_negative_derivative(max("metrics_written"), 1s) AS "written"'], [],
                       "Output buffer utilisation"),
}

KUBERNETES_QUERIES = {
    "kubernetes_pod_container": (['sum("phase_type") AS "containers"'], ["namespace"], "Container count by namespace"),
    "kubernetes_node": (['max("ready") AS "ready"'], ["node_name"], "Node ready condition"),
    "kubernetes_service": (['count("ready") AS "services"'], ["namespace"], "Service count by namespace"),
    "kubernetes_endpoint": (['count("ready") AS "endpoints"'], ["namespace"], "Endpoint count by namespace"),
    "kubernetes_persistentvolume": (['count("phase_type") AS "volumes"'], ["phase"], "PVs by phase"),
}


def generic_rule(name: str, fields: dict[str, str]) -> tuple[list[str], str]:
    """Rate for counters, value for gauges, interval average for histograms."""
    if "counter" in fields:
        unit = unit_for(name)
        return [rate_expr("counter", "rate")], unit
    if "gauge" in fields:
        agg = "max" if "_hwm" in name else "mean"
        expr = f'{agg}("gauge") AS "value"'
        if any(k in name for k in ("bytes", "memory", "mem_")):
            return [expr], "bytes"
        if "seconds" in name or "poll_time" in name or "duration" in name:
            return [expr], "s"
        return [expr], "short"
    if "sum" in fields and "count" in fields:
        expr = ('non_negative_derivative(max("sum"), 1s) / '
                'non_negative_derivative(max("count"), 1s) AS "avg"')
        if any(k in name for k in ("seconds", "duration", "latency", "ttfb", "wait")):
            return [expr], "s"
        if "_bytes" in name or "bytes" in name:
            return [expr], "bytes"
        return [expr], "short"
    numeric = [c for c, t in fields.items() if t in ("Int64", "Float64", "UInt64")]
    if numeric:
        return [f'max("{numeric[0]}") AS "value"'], unit_for(name)
    return [], "short"


def build_targets(name: str, fields: dict[str, str], tags: list[str]):
    """One target per measurement plus its panel metadata."""
    if name in HOST_QUERIES:
        exprs, group, desc = HOST_QUERIES[name]
        unit = {"cpu": "percent", "mem": "percent", "swap": "percent", "disk": "percent",
                "diskio": "ops", "net": "bps", "kernel": "ops",
                "processes": "short", "system": "short"}[name]
        extra = ' AND "cpu" = \'cpu-total\'' if name == "cpu" else ""
    elif name in SEAWEEDFS_QUERIES:
        exprs, group, desc = SEAWEEDFS_QUERIES[name]
        unit = "bytes" if "bytes" in " ".join(exprs) else (
            "percent" if "used %" in " ".join(exprs) else "short")
        extra = ""
    elif name in INTERNAL_QUERIES:
        exprs, group, desc = INTERNAL_QUERIES[name]
        unit = "bytes" if "bytes" in " ".join(exprs) else (
            "percent" if "%" in " ".join(exprs) else "ops")
        extra = ""
    elif name in KUBERNETES_QUERIES:
        exprs, group, desc = KUBERNETES_QUERIES[name]
        unit = "short"
        extra = ""
    else:
        exprs, unit = generic_rule(name, fields)
        # high-cardinality families: aggregate across pods/urls instead of grouping
        group = []
        desc = name.replace("_", " ")
        extra = ""
    if not exprs:
        return None, unit
    group_clause = (", " + ", ".join(qname(t) for t in group)) if group else ""
    target = (f"SELECT {', '.join(exprs)} FROM {qname(name)} "
              f"WHERE $timeFilter{extra} GROUP BY time($__interval){group_clause} fill(null)")
    return {"query": target, "group": group, "desc": desc}, unit


# ------------------------------------------------------------------ panels
UNIT_LABEL = {
    "percent": "utilisation",
    "bps": "network throughput",
    "ops": "rates",
    "short": "counts",
    "bytes": "memory & storage",
    "s": "durations",
    "reqps": "request rates",
    "dateTimeAsIso": "timestamps",
}


# ------------------------------------------------------------- descriptions
# Prose for each generated panel, keyed by the exact title build() produces.
# The fallback is the machine listing of measurement names, so a panel added to
# a new group still ships with *something* truthful.
GROUP_DESCRIPTIONS = {
    "Host / OS": "Telegraf's host inputs: cpu, mem, swap, disk, diskio, net, kernel, "
                 "processes and system.",
    "SeaweedFS": "SeaweedFS input from this repo's plugin: cluster, disk, collection, volume "
                 "and volume-server measurements.",
    "Telegraf": "Telegraf internal_* inputs — the agent's own health: throughput, heap, gather "
                "time and buffer pressure.",
    "Kubernetes": "kubernetes_* measurements from Telegraf's kube_inventory input.",
    "InfluxDB 3 — ingest & writes": "Counters and gauges from the ingest and write path of "
                                    "InfluxDB 3 Enterprise.",
    "InfluxDB 3 — WAL & snapshots": "WAL flush and snapshot creation on the ingest nodes.",
    "Query path": "HTTP request handling and the query log: plan → execute → ingester → result.",
    "Memory & process": "Allocator pools and process identity.",
    "Async semaphore": "IOx async semaphore that caps query concurrency (holders and permits).",
    "Object store": "Object-store (S3) traffic between InfluxDB 3 and SeaweedFS.",
    "Tokio & failures": "Rust runtime health: queues, workers, panics and hang counters.",
}

PANEL_DESCRIPTIONS = {
    # ---- host / os
    "Host / OS — network throughput":
        "net: rx/tx in bits/s, summed across interfaces (bytes_recv/bytes_sent × 8 as a "
        "non-negative rate).",
    "Host / OS — rates":
        "diskio: read and write IOPS summed across devices. kernel: context switches, "
        "interrupts and forks per second.",
    "Host / OS — CPU utilisation":
        "CPU busy = 100 − usage_idle on cpu-total, per host.",
    "Host / OS — memory utilisation":
        "Memory used_percent per host.",
    "Host / OS — disk utilisation":
        "Disk space and inode used_percent per host and mount.",
    "Host / OS — counts":
        "processes: running, blocked, zombie and total counts. system: load1/5/15 and uptime.",
    # ---- seaweedfs
    "SeaweedFS — memory & storage":
        "seaweedfs_collection: size_bytes and file_count per collection. seaweedfs_disk: "
        "used_percent and free_bytes per volume-server data path.",
    "SeaweedFS — counts":
        "seaweedfs_cluster: volumes, total slots, free slots and server count. seaweedfs_master: "
        "highest volume ID assigned. seaweedfs_volume_server: per-server volumes and free slots.",
    # ---- telegraf internal
    "Telegraf — memory & storage":
        "internal_memstats: heap in use, heap reserved from the OS, and cumulative allocations.",
    "Telegraf — rates":
        "internal_agent: metrics gathered, written and dropped, plus write errors per second. "
        "internal_gather: gather duration per input. internal_process: agent CPU and RSS. "
        "internal_prometheus: scrape connection state (1 = connected). internal_statsd: pending "
        "StatsD messages.",
    "Telegraf — utilisation":
        "internal_write: output buffer fill (buffer_size / buffer_limit as %) and the "
        "metrics-written rate. Buffer % pinned near 100 means the output cannot keep up.",
    # ---- kubernetes
    "Kubernetes":
        "kubernetes_endpoint: endpoints per namespace. kubernetes_node: ready (1 = Ready). "
        "kubernetes_persistentvolume: PVs by phase. kubernetes_pod_container: containers per "
        "namespace. kubernetes_service: services per namespace.",
    # ---- ingest & writes
    "InfluxDB 3 — ingest & writes — memory & storage":
        "influxdb3_file_cache_fetched_bytes_total: file-cache fetch rate. file_cache_size_bytes: "
        "cache size. ingest_lp_bytes_received_total: line-protocol bytes received per second — "
        "the true inbound rate. ingest_pre_snapshot_buffer_bytes and "
        "ingest_pre_wal_flush_buffer_bytes: bytes buffered before snapshot and WAL flush. "
        "write_bytes_total: bytes written per second.",
    "InfluxDB 3 — ingest & writes — rates":
        "file_cache_access_total: cache accesses per second. ingest_writes_total: ingest write "
        "operations per second. node_identity_lease_renewal_failures_total and "
        "..._renewals_after_window_closed_total: catalog lease renewal failures — anything "
        "non-zero means nodes are losing their lease.",
    "InfluxDB 3 — ingest & writes — durations":
        "lease_clock_offset_seconds: measured clock offset used by the identity lease. Large or "
        "oscillating values cause lease churn.",
    "InfluxDB 3 — ingest & writes — counts (1)":
        "file_cache_entries_count: cached file entries. ingest_pre_snapshot_buffer_rows/_tables "
        "and ingest_pre_wal_flush_buffer_rows/_tables: rows and tables buffered before snapshot "
        "and WAL flush — sustained growth means the flush cannot keep up. "
        "ingest_write_concurrency: in-flight ingest writes.",
    "InfluxDB 3 — ingest & writes — counts (2)":
        "node_identity_lease_term: current lease term, incremented on every failover. "
        "write_lines_total rate: line-protocol lines written per second.",
    # ---- WAL & snapshots
    "InfluxDB 3 — WAL & snapshots — memory & storage":
        "snapshot_bytes_written_total: bytes per second written into snapshot files. "
        "wal_bytes_flushed_total: bytes per second flushed out of the WAL.",
    "InfluxDB 3 — WAL & snapshots — rates":
        "snapshot_total: snapshots created per second. wal_flush_total: WAL flush operations "
        "per second.",
    "InfluxDB 3 — WAL & snapshots — durations":
        "Average snapshot construction time and average WAL flush latency (derivative of sum ÷ "
        "derivative of count).",
    "InfluxDB 3 — WAL & snapshots — counts (1)":
        "snapshot_files_written_total, snapshot_rows_written_total and "
        "snapshot_rows_by_window_age_total: per-second snapshot write volume. snapshot_windows: "
        "average windows per snapshot. snapshot_windows_seen_24h: windows observed in the last "
        "24 h. wal_flush_concurrency: concurrent WAL flushes.",
    "InfluxDB 3 — WAL & snapshots — counts (2)":
        "wal_rows_flushed_total and wal_tables_flushed_total per second — volume and table "
        "fan-out of WAL flushing.",
    # ---- query path
    "Query path — memory & storage":
        "http_response_body_size_bytes: average response body size (derivative of sum ÷ "
        "derivative of count).",
    "Query path — rates":
        "influxdb_iox_query_log_phase_entered_total: query phase transitions per second. "
        "query_datafusion_query_execution_ooms_total: queries killed by DataFusion memory "
        "exhaustion — must stay at 0.",
    "Query path — request rates":
        "http_requests_total per second — total API request rate, writes and queries together.",
    "Query path — durations (1)":
        "Average HTTP request duration plus query-log phase timings: compute, end-to-end, "
        "execute, and ingester latency to data / to plan.",
    "Query path — durations (2)":
        "Plan-phase timings: logical plan, permit wait, physical plan and total plan duration, "
        "averaged per query.",
    "Query path — counts (1)":
        "Average per query: deduplicated parquet files and partitions, ingester partition "
        "count, ingester response rows and size, and peak memory.",
    "Query path — counts (2)":
        "Average per query: rows returned, parquet files read, partitions scanned. "
        "phase_current: queries currently inside a phase.",
    # ---- memory & process
    "Memory & process — memory & storage":
        "datafusion_mem_pool_bytes: memory held by the DataFusion pool. jemalloc_memstats_bytes: "
        "allocator statistics.",
    "Memory & process — durations":
        "process_start_time_seconds shown as a timestamp — a jump means the process restarted.",
    # ---- async semaphore
    "Async semaphore — rates":
        "Holders and permits cancelled while pending, per second — work that gave up waiting "
        "for a permit. Sustained non-zero means the semaphore is the bottleneck.",
    "Async semaphore — durations":
        "Average semaphore acquire time and average first-poll wait — queueing delay in front "
        "of the permit.",
    "Async semaphore — counts (1)":
        "Holders and permits acquired, current and high-water (…_hwm = peak since process "
        "start).",
    "Async semaphore — counts (2)":
        "Permits pending now, their high-water mark, and the total permits configured.",
    # ---- object store
    "Object store — memory & storage":
        "object_store_transfer_bytes_hist: average bytes per transfer. transfer_bytes_total: "
        "bytes per second.",
    "Object store — durations":
        "Average object-store operation duration, time to response headers, and time to first "
        "byte.",
    "Object store — counts":
        "Average objects per transfer and transfer operations per second.",
    # ---- tokio
    "Tokio & failures — rates (1)":
        "thread_panic_count_total: panics — must stay at 0; any growth is a bug. Then budget "
        "forced yields, IO-driver fd register/deregister/ready events, and remote schedules "
        "per second.",
    "Tokio & failures — rates (2)":
        "tokio_watchdog_hangs_total: must stay 0. Also worker local schedules, noop polls, "
        "overflows, parks and polls per second.",
    "Tokio & failures — rates (3)":
        "Worker steal events and steal operations per second.",
    "Tokio & failures — durations":
        "Average watchdog response time, mean poll time per task, and the rate of total worker "
        "busy time — i.e. how much worker capacity is in use.",
    "Tokio & failures — counts (1)":
        "Runtime queue depths (blocking and global), alive tasks, and blocking / worker / idle "
        "thread counts.",
    "Tokio & failures — counts (2)":
        "Per-worker local queue depth.",
}


# Panel titles gain a " (N)" suffix when a family needs more than one panel.
# Chunking shifts as measurements appear, so those families get one prose block
# keyed by the base title, never per chunk.
FAMILY_DESCRIPTIONS = {
    "InfluxDB 3 — ingest & writes — counts":
        "Gauges and counters from the ingest/write path: file-cache entries, rows and tables "
        "buffered before snapshot and WAL flush, in-flight write concurrency, node identity "
        "lease term, and the line write rate. Buffered rows climbing while writes continue "
        "means the snapshot or WAL flush cannot keep up.",
    "InfluxDB 3 — WAL & snapshots — counts":
        "Per-second snapshot and WAL flushing volume: files and rows written, rows by window "
        "age, average windows per snapshot, windows seen in the last 24 h, WAL flush "
        "concurrency, and rows/tables flushed out of the WAL.",
    "Query path — durations":
        "Average query timings across the request and query-log phases: HTTP request, compute, "
        "end-to-end, execute, ingester latency to data and to plan, logical/physical/total "
        "plan, and permit wait.",
    "Query path — counts":
        "Average per query: deduplicated parquet files and partitions, ingester partition "
        "count, ingester response rows and size, peak memory, rows returned, parquet files "
        "read, partitions scanned, plus the current query phase.",
    "Async semaphore — counts":
        "Semaphore occupancy: holders and permits acquired, pending, and their high-water "
        "marks (…_hwm = peak since process start), plus the total permits configured.",
    "Tokio & failures — rates":
        "Runtime event rates: panics (must be 0), watchdog hangs (must be 0), budget forced "
        "yields, IO-driver fd registration and readiness, remote and local schedules, noop "
        "polls, overflow, park, poll and steal counters.",
    "Tokio & failures — counts":
        "Runtime occupancy: blocking and global queue depth, alive tasks, worker / blocking / "
        "idle thread counts, and per-worker local queue depth.",
}


def thresholds(unit: str):
    if unit == "percent":
        return {"mode": "absolute",
                "steps": [{"color": "green", "value": None},
                          {"color": "yellow", "value": 70}, {"color": "red", "value": 85}]}
    if unit == "s":
        return {"mode": "absolute",
                "steps": [{"color": "green", "value": None},
                          {"color": "yellow", "value": 0.25}, {"color": "red", "value": 1}]}
    return {"mode": "absolute", "steps": [{"color": "green", "value": None}]}


def timeseries_panel(title, targets, description, unit, grid_pos, ref_start=0,
                     steps=None):
    return {
        "type": "timeseries", "title": title, "description": description,
        "datasource": DS,
        "targets": [{"refId": chr(ord("A") + ref_start + i), "query": t["query"],
                     "rawQuery": True, "resultFormat": "time_series",
                     "datasource": DS, "intervalMs": 60000, "maxDataPoints": 300}
                    for i, t in enumerate(targets)],
        "gridPos": grid_pos,
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": 2,
            "color": {"mode": "palette-classic"},
            "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 6,
                       "showPoints": "never", "spanNulls": True,
                       "axisPlacement": "auto", "axisBorderShow": False,
                       "thresholdsStyle": {"mode": "dashed"}},
            "thresholds": {"mode": "absolute",
                           "steps": steps or thresholds(unit)["steps"]}}, "overrides": []},
        # Legend at the bottom of every cell: a right-hand table squeezed the plot area on
        # the wide panels and made rows of panels look different from each other.
        "options": {"legend": {"displayMode": "list", "placement": "bottom",
                               "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def stat_panel(title, query, unit, grid_pos, steps=None, desc=""):
    return {
        "type": "stat", "title": title, "description": desc, "datasource": DS,
        "targets": [{"refId": "A", "query": query, "rawQuery": True,
                     "resultFormat": "time_series", "datasource": DS,
                     "intervalMs": 60000, "maxDataPoints": 300}],
        "gridPos": grid_pos,
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": 1,
            "color": {"mode": "thresholds"},
            # Grafana wants {"mode", "steps"}; passing a bare list here silently makes it
            # ignore the thresholds (which is why the summary cards never coloured).
            "thresholds": {"mode": "absolute", "steps": steps} if steps else thresholds(unit)},
            "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background_gradient", "graphMode": "area",
                    "justifyMode": "auto", "orientation": "auto", "textMode": "auto"},
        "transformations": [
            {"id": "renameByRegex",
             "options": {"regex": r"^[^ ]+ \{ \w+: (.*) \}$", "renamePattern": "$1"}},
            {"id": "renameByRegex",
             "options": {"regex": r"^[a-z0-9_]+\.[a-z0-9_]+$", "renamePattern": "value"}},
        ],
    }


EXPANDED_GROUPS = {"Host / OS", "SeaweedFS"}

# Some host metrics earn their own panel instead of sharing the unit-based one: cpu, memory
# and disk utilisation answer different questions and mixing them made the legend
# unreadable. Panel sizes are uniform (PANEL_WIDTH x PANEL_HEIGHT) so every row lines up.
PANEL_SPLIT = {
    "cpu": "CPU utilisation",
    "mem": "memory utilisation",
    "disk": "disk utilisation",
}

# One cell size for every timeseries panel: two per row of 24 columns, and a group with an
# odd number of panels lets its last cell span the full width so no row ends half empty.
PANEL_WIDTH = 12
PANEL_HEIGHT = 9

# Measurements deliberately left off the dashboard, with the reason. They still count as
# covered, so the completeness check keeps its meaning.
SKIP_MEASUREMENTS = {
    "swap": "swap is unused on these nodes; the panel was dropped in dashboard v12",
    "seaweedfs_volume": "one series per volume — charted on the 'SeaweedFS — Volumes' board",
}

# Whole families that are curated by hand instead of auto-charted: the compactor exposes
# ~70 metrics, and five panels answering real questions beat seventy time series nobody
# reads. See the "InfluxDB 3 — Compaction (Pacha Tree)" section at the end of build().
SKIP_PREFIXES = {
    "influxdb3_compactor_": "curated in the Compaction (Pacha Tree) section",
}


def row(title, y, collapsed=False, desc=""):
    return {"type": "row", "title": title, "collapsed": collapsed, "description": desc,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


# ------------------------------------------------------------------- build
def build(schema, tags, fields):
    measurements = sorted(schema)
    units: dict[str, str] = {}
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    bucket_units: dict[tuple[str, str], str] = {}
    coverage: list[str] = []

    for name in measurements:
        skip_reason = SKIP_MEASUREMENTS.get(name) or next(
            (why for prefix, why in SKIP_PREFIXES.items() if name.startswith(prefix)), None)
        if skip_reason:
            print(f"  - not charted: {name} — {skip_reason}", file=sys.stderr)
            coverage.append(name)
            continue
        target, unit = build_targets(name, fields[name], tags[name])
        if target is None:
            print(f"  ! no numeric field, skipped: {name}", file=sys.stderr)
            continue
        group = classify(name, fields[name])[0]
        short = name
        target["label"] = short
        target["measurement"] = name
        label = PANEL_SPLIT.get(name, UNIT_LABEL.get(unit, unit))
        buckets[(group, label)].append(target)
        bucket_units[(group, label)] = unit
        units[name] = unit
        coverage.append(name)

    panels = []
    y = 0
    # ---- summary row
    summary = [
        ("InfluxDB 3 ingest", 'SELECT non_negative_derivative(max("counter"), 1s) AS "lines" '
                              'FROM "influxdb3_write_lines_total" WHERE $timeFilter '
                              'GROUP BY time($__interval)',
         "ops", [("green", None)], "Line-protocol lines written per second"),
        ("CPU busy", 'SELECT 100 - mean("usage_idle") AS "busy" FROM "cpu" '
                     'WHERE $timeFilter AND "cpu" = \'cpu-total\' '
                     'GROUP BY time($__interval), "host"',
         "percent", [("green", None), ("yellow", 70), ("red", 85)], "Busiest host, latest value"),
        ("Memory used", 'SELECT mean("used_percent") AS "used" FROM "mem" '
                        'WHERE $timeFilter GROUP BY time($__interval), "host"',
         "percent", [("green", None), ("yellow", 80), ("red", 92)], "Highest host memory usage"),
        ("Disk used", 'SELECT max("used_percent") AS "used" FROM "disk" WHERE $timeFilter '
                      'AND "path" = \'/\' GROUP BY time($__interval), "host"',
         "percent", [("green", None), ("yellow", 80), ("red", 90)], "Root filesystem per node"),
        ("SeaweedFS slots free", 'SELECT max("free_volume_slots") AS "free" FROM "seaweedfs_cluster" '
                                 'WHERE $timeFilter GROUP BY time($__interval)',
         "short", [("red", None), ("yellow", 50), ("green", 200)],
         "Free volume slots across both data dirs (1720 total, 4 GB each). At 0 the master "
         "cannot grow a volume and S3 writes fail; ~1.7 volumes/hour burn makes 50 about a day"),
        ("Telegraf write errors", 'SELECT non_negative_derivative(max("write_errors"), 1s) AS "errors" '
                                  'FROM "internal_agent" WHERE $timeFilter GROUP BY time($__interval)',
         "ops", [("green", None), ("red", 0.0001)], "Output write error rate"),
    ]
    for i, (title, query, unit, steps, desc) in enumerate(summary):
        panels.append(stat_panel(title, query, unit,
                                 {"x": (i % 3) * 8, "y": i // 3 * 5, "w": 8, "h": 5},
                                 steps=[{"color": c, "value": v} for c, v in steps], desc=desc))
    y = 10

    order = ["Host / OS", "SeaweedFS", "Telegraf", "Kubernetes",
             "InfluxDB 3 — ingest & writes", "InfluxDB 3 — WAL & snapshots",
             "Query path", "Memory & process", "Async semaphore", "Object store",
             "Tokio & failures", "Other"]
    for group in order:
        keys = sorted(k for k in buckets if k[0] == group)
        if not keys:
            continue
        collapsed = group not in EXPANDED_GROUPS
        specs = []
        for label in dict.fromkeys(k[1] for k in keys):
            targets = buckets[(group, label)]
            for chunk_start in range(0, len(targets), 6):
                chunk = targets[chunk_start:chunk_start + 6]
                title = group
                if len(keys) > 1:
                    title += f" — {label}"
                if len(targets) > 6:
                    title += f" ({chunk_start // 6 + 1})"
                base = re.sub(r" \(\d+\)$", "", title)
                if base != title and base in FAMILY_DESCRIPTIONS:
                    desc = FAMILY_DESCRIPTIONS[base]
                else:
                    desc = PANEL_DESCRIPTIONS.get(
                        title,
                        "; ".join(f"{t['measurement']}: {t['desc']}" for t in chunk))
                specs.append((title, chunk, desc, bucket_units.get((group, label), "none")))

        # Lay the group out on a fixed grid rather than packing mixed widths: two equal
        # cells per row, and a lone trailing cell takes the full width. Mixed widths used
        # to wrap mid-row and leave half of a row empty (the old 8/12/6 mix in Host / OS).
        group_panels = []
        for i, (title, chunk, desc, unit) in enumerate(specs):
            alone = i == len(specs) - 1 and i % 2 == 0
            group_panels.append(timeseries_panel(
                title, chunk, desc, unit,
                {"x": 0 if i % 2 == 0 else PANEL_WIDTH,
                 "y": y + 1 + (i // 2) * PANEL_HEIGHT,
                 "w": 24 if alone else PANEL_WIDTH,
                 "h": PANEL_HEIGHT}))
        rows = (len(specs) + 1) // 2

        row_panel = row(group, y, collapsed=collapsed,
                        desc=GROUP_DESCRIPTIONS.get(group, ""))
        if collapsed:
            # nested panels are only laid out/queried when the row is opened
            row_panel["panels"] = group_panels
            panels.append(row_panel)
            y += 1
        else:
            panels.append(row_panel)
            panels.extend(group_panels)
            y = y + 1 + rows * PANEL_HEIGHT


    # ---- InfluxDB 3 compaction (Pacha Tree): the compactor's own /metrics, which the
    # Telegraf prometheus input only started scraping when the second target was added.
    # Without these panels a compaction backlog is invisible until queries slow down.
    panels.append(row("InfluxDB 3 — Compaction (Pacha Tree)", y, collapsed=False,
                      desc="Compaction health from the compactor's /metrics. Backlog pins "
                           "gen0 files into every query scan, so lag is a query-cost "
                           "problem before it is a storage problem."))
    y += 1
    panels.append(timeseries_panel(
        "Compaction lag — oldest uncompacted snapshot",
        [{"query": 'SELECT max("gauge") AS "lag_s" FROM "influxdb3_compactor_snapshot_lag_seconds" '
                   'WHERE $timeFilter GROUP BY time($__interval), "node_id" fill(previous)',
          "desc": "per ingest node"}],
        "Age of the oldest snapshot the compactor has not yet merged, per ingest node "
        "(influxdb3_compactor_snapshot_lag_seconds, scraped from the compactor's /metrics). "
        "Do not alert on this alone: the metric reports 0 for a node that has never "
        "compacted, so a brand-new node looks perfect while holding a backlog. Alert on "
        "uncompacted snapshots.",
        "s", {"x": 0, "y": y, "w": 12, "h": PANEL_HEIGHT},
        steps=[{"color": "green", "value": None}, {"color": "yellow", "value": 60},
               {"color": "red", "value": 300}]))
    panels.append(timeseries_panel(
        "Uncompacted snapshots (backlog pinning gen0 into every scan)",
        [{"query": 'SELECT max("gauge") AS "uncompacted" FROM "influxdb3_compactor_uncompacted_snapshots" '
                   'WHERE $timeFilter GROUP BY time($__interval), "node_id" fill(previous)',
          "desc": "per ingest node"}],
        "Snapshots discovered but not compacted, per ingest node. Each one pins its gen0 "
        "files into every query's scan set — this is the real cost of lag. Sustained >5 is "
        "worth a look; >20 means queries are reading a wide file set.",
        "short", {"x": 12, "y": y, "w": 12, "h": PANEL_HEIGHT},
        steps=[{"color": "green", "value": None}, {"color": "yellow", "value": 5},
               {"color": "red", "value": 20}]))
    y += PANEL_HEIGHT
    panels.append(stat_panel(
        "Compactor primary",
        'SELECT last("gauge") AS "primary" FROM "influxdb3_compactor_is_primary" WHERE $timeFilter',
        "none", {"x": 0, "y": y, "w": 8, "h": PANEL_HEIGHT},
        steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
        desc="1 = this node holds the compactor lease and is scheduling compaction. No node "
             "reporting 1 means nothing is compacting; more than one for longer than a lease "
             "term means split-brain."))
    panels.append(stat_panel(
        "Deferred snapshots",
        'SELECT last("gauge") AS "deferred" FROM "influxdb3_compactor_deferred_snapshots" '
        'WHERE $timeFilter GROUP BY "node_id"',
        "short", {"x": 8, "y": y, "w": 8, "h": PANEL_HEIGHT},
        steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        desc="Snapshots that failed to compact and are accumulating. Non-zero is a gate, not "
             "just a symptom: --recover-deferred-snapshots only runs while every ingest node "
             "has fewer in-flight snapshots than --recover-deferred-snapshots-in-flight-limit "
             "(default 3), which a busy cluster never drops below. Raise the limit to 32+ to "
             "let recovery drain."))
    panels.append(timeseries_panel(
        "Compactor memory — used vs budget",
        [{"query": 'SELECT max("gauge") AS "used" FROM "influxdb3_compactor_memory_used_bytes" '
                   'WHERE $timeFilter GROUP BY time($__interval) fill(previous)', "desc": "in flight"},
         {"query": 'SELECT max("gauge") AS "budget" FROM "influxdb3_compactor_memory_budget_bytes" '
                   'WHERE $timeFilter GROUP BY time($__interval) fill(previous)', "desc": "admission budget"}],
        "Admission budget (--compactor-input-size-budget, default 50% of RAM) vs bytes in "
        "flight. Used pinned at the budget while uncompacted snapshots climb means capacity, "
        "not a stall: raise the budget or add a compact node.",
        "bytes", {"x": 16, "y": y, "w": 8, "h": PANEL_HEIGHT}))
    y += PANEL_HEIGHT

    dash = {
        "uid": DASH_UID, "title": DASH_TITLE,
        "tags": ["telegraf", "influxdb3", "infrastructure", "seaweedfs"],
        "timezone": "browser", "editable": True, "graphTooltip": 1,
        "refresh": "1m", "time": {"from": "now-3h", "to": "now"},
        "schemaVersion": 39, "panels": panels,
        "templating": {"list": []},
        "annotations": {"list": []},
    }
    for i, p in enumerate(panels, start=1):
        p["id"] = i
    return dash, coverage, measurements, units


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    global DS
    DS = {"type": "influxdb", "uid": datasource_uid()}
    schema, tags, fields = load_schema()
    dash, coverage, measurements, units = build(schema, tags, fields)

    missing = sorted(set(measurements) - set(coverage))
    duplicates = len(coverage) != len(set(coverage))
    print(f"measurements in bucket : {len(measurements)}")
    print(f"measurements covered   : {len(set(coverage))}")
    print(f"panels                 : {len(dash['panels'])}")
    print(f"missing                : {missing or 'none'}")
    print(f"duplicates             : {duplicates}")
    if missing:
        sys.exit("refusing to publish: some measurements have no panel")

    # overlap check
    taken = {}
    flat = []
    for p in dash["panels"]:
        flat.append(p)
        flat.extend(p.get("panels") or []) if p.get("type") == "row" else None
    for p in [q for q in dash["panels"] if q.get("type") != "row"]:
        g = p["gridPos"]
        for yy in range(g["y"], g["y"] + g["h"]):
            for xx in range(g["x"], g["x"] + g["w"]):
                if (xx, yy) in taken:
                    sys.exit(f"layout overlap: {p['title']} vs {taken[(xx, yy)]}")
                taken[(xx, yy)] = p["title"]
    print("layout                 : no overlaps")

    if args.dry_run:
        print(json.dumps(dash)[:500])
        return

    code, body = grafana("POST", "/api/dashboards/db",
                         {"dashboard": dash, "overwrite": True,
                          "message": "generated by build_all_measurements_dashboard.py"})
    if code != 200:
        sys.exit(f"dashboard import failed: {code} {body}")
    print(f"dashboard              : {body.get('uid')} v{body.get('version')} {body.get('url')}")

    if args.verify:
        failures = 0
        for p in dash["panels"]:
            for t in p.get("targets", []):
                code, resp = grafana("POST", "/api/ds/query", {
                    "queries": [{"refId": "A", "datasource": DS, "query": t["query"],
                                 "rawQuery": True, "resultFormat": "table",
                                 "intervalMs": 60000, "maxDataPoints": 300}],
                    "from": "now-3h", "to": "now"})
                res = (resp.get("results") or {}).get("A", {}) if code == 200 else {}
                if code != 200 or "error" in res:
                    failures += 1
                    print(f"  FAIL {p['title']}: {str(res.get('error') or resp)[:180]}")
        print(f"query verification     : {'all ok' if failures == 0 else f'{failures} failed'}")
        if failures:
            sys.exit(1)


DS: dict = {}
if __name__ == "__main__":
    main()
