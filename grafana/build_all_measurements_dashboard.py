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


def timeseries_panel(title, targets, description, unit, grid_pos, ref_start=0):
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
            "thresholds": thresholds(unit)}, "overrides": []},
        "options": {"legend": {"displayMode": "table",
                               "placement": "bottom" if len(targets) >= 4 else "right",
                               "showLegend": True, "calcs": ["lastNotNull", "max"]},
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
            "thresholds": steps or thresholds(unit)}, "overrides": []},
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


def row(title, y, collapsed=False):
    return {"type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


# ------------------------------------------------------------------- build
def build(schema, tags, fields):
    measurements = sorted(schema)
    units: dict[str, str] = {}
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    coverage: list[str] = []

    for name in measurements:
        target, unit = build_targets(name, fields[name], tags[name])
        if target is None:
            print(f"  ! no numeric field, skipped: {name}", file=sys.stderr)
            continue
        group = classify(name, fields[name])[0]
        short = name
        target["label"] = short
        target["measurement"] = name
        buckets[(group, unit)].append(target)
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
         "short", [("red", None), ("yellow", 1), ("green", 5)], "When this hits 0, S3 writes fail"),
        ("Telegraf write errors", 'SELECT non_negative_derivative(max("write_errors"), 1s) AS "errors" '
                                  'FROM "internal_agent" WHERE $timeFilter GROUP BY time($__interval)',
         "ops", [("green", None), ("red", 0.0001)], "Output write error rate"),
    ]
    for i, (title, query, unit, steps, desc) in enumerate(summary):
        panels.append(stat_panel(title, query, unit,
                                 {"x": (i % 4) * 6, "y": i // 4 * 5, "w": 6, "h": 5},
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
        group_panels = []
        col, row_y, max_h = 0, y, 0
        for unit in dict.fromkeys(k[1] for k in keys):
            targets = buckets[(group, unit)]
            for chunk_start in range(0, len(targets), 6):
                chunk = targets[chunk_start:chunk_start + 6]
                title = group
                if len(keys) > 1:
                    title += f" — {UNIT_LABEL.get(unit, unit)}"
                if len(targets) > 6:
                    title += f" ({chunk_start // 6 + 1})"
                desc = "; ".join(f"{t['measurement']}: {t['desc']}" for t in chunk)
                group_panels.append(timeseries_panel(
                    title, chunk, desc, unit, {"x": col, "y": row_y, "w": 12, "h": 7}))
                max_h = max(max_h, 7)
                col += 12
                if col >= 24:
                    col, row_y = 0, row_y + max_h
                    max_h = 0
        row_panel = row(group, y, collapsed=collapsed)
        if collapsed:
            # nested panels are only laid out/queried when the row is opened
            row_panel["panels"] = group_panels
            panels.append(row_panel)
            y += 1
        else:
            panels.append(row_panel)
            panels.extend(group_panels)
            y = row_y + (max_h or 7)

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
