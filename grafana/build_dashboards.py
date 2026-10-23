#!/usr/bin/env python3
"""Create the SeaweedFS dashboards in Grafana from the lab's Telegraf data.

Reads the Grafana admin credentials and the InfluxDB 3 token from the cluster
secrets, points a dashboard at the existing InfluxDB datasource (InfluxQL mode)
and creates/updates two dashboards in a "SeaweedFS" folder.

Usage:
  kubectl -n grafana port-forward svc/grafana 3000:80 &
  kubectl -n influxdb3 port-forward svc/influxdb3-querier 8181:8181 &
  python3 build_dashboards.py            # create/update
  python3 build_dashboards.py --verify   # also run every panel query

Environment overrides: GRAFANA_URL, DS_UID, KUBECONFIG.
"""
import argparse
import base64
import json
import os
import subprocess
import urllib.error
import urllib.request

GRAFANA = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
KUBECONFIG = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))


def secret(ns, name, key):
    return subprocess.check_output(
        ["kubectl", "-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        env={**os.environ, "KUBECONFIG": KUBECONFIG},
    ).decode().strip()


GU = base64.b64decode(secret("grafana", "grafana", "admin-user")).decode()
GP = base64.b64decode(secret("grafana", "grafana", "admin-password")).decode()
TOKEN = base64.b64decode(secret("influxdb3", "telegraf-influxdb3-token", "TOKEN")).decode()
AUTH = base64.b64encode(f"{GU}:{GP}".encode()).decode()


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        GRAFANA + path, data=data, method=method,
        headers={"Authorization": "Basic " + AUTH, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


# ---------------------------------------------------------------- datasource
def ensure_datasource():
    """InfluxDB 3 querier in InfluxQL mode. The v3 query endpoint authenticates
    with a token header, which this datasource only sends via a custom header."""
    _, existing = call("GET", "/api/datasources")
    for ds in existing or []:
        if ds["name"] == "InfluxDB3 telegraf":
            return ds["uid"]
    code, body = call("POST", "/api/datasources", {
        "name": "InfluxDB3 telegraf",
        "type": "influxdb",
        "access": "proxy",
        "url": os.environ.get(
            "INFLUX_URL",
            "http://influxdb3-querier.influxdb3.svc.cluster.local:8181"),
        "isDefault": True,
        "jsonData": {"version": "InfluxQL", "dbName": "telegraf", "httpMode": "POST",
                     "httpHeaderName1": "Authorization"},
        "secureJsonData": {"httpHeaderValue1": "Token " + TOKEN},
    })
    if code != 200:
        raise SystemExit(f"datasource create failed: {code} {body}")
    print("datasource created:", body["datasource"]["uid"])
    return body["datasource"]["uid"]


DS = {"type": "influxdb", "uid": os.environ.get("DS_UID") or ensure_datasource()}


def ensure_folder():
    _, folders = call("GET", "/api/folders")
    for f in folders or []:
        if f["title"] == "SeaweedFS":
            return f["uid"]
    code, body = call("POST", "/api/folders", {"title": "SeaweedFS"})
    if code != 200:
        raise SystemExit(f"folder create failed: {code} {body}")
    return body["uid"]


# ------------------------------------------------------------------- helpers
def target(sql, fmt="time_series", ref="A"):
    return {"refId": ref, "query": sql, "rawQuery": True, "resultFormat": fmt,
            "datasource": DS, "intervalMs": 60000, "maxDataPoints": 500}


def thresh(steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def rename_regex(pattern, replacement):
    """InfluxQL series come back as "measurement.field { tag: value }" without
    labels, so clean the legend/title text with a regex rename."""
    return {"id": "renameByRegex", "options": {"regex": pattern, "renamePattern": replacement}}


COLLECTION_RE = rename_regex(r".*\{ collection: (.*) \}", "$1")
PATH_RE = rename_regex(r".*\{ path: (.*) \}", "$1")
CLUSTER_RE = rename_regex(r"^seaweedfs_cluster\.(.*)$", "$1")
SERVER_RE = rename_regex(r"seaweedfs_volume_server\.(\w+) \{ server: https?://([^.]+)\..* \}", "$1 ($2)")


def grid(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def stat(title, sql, pos, unit="none", decimals=1, steps=None, mn=None, mx=None,
         graph="area", desc="", mappings=None, fmt="time_series"):
    defaults = {"unit": unit, "decimals": decimals, "color": {"mode": "thresholds"},
                "thresholds": thresh(steps or [("text", None)])}
    if mn is not None:
        defaults["min"] = mn
    if mx is not None:
        defaults["max"] = mx
    if mappings:
        defaults["mappings"] = mappings
    return {
        "type": "stat", "title": title, "description": desc, "datasource": DS,
        "targets": [target(sql, fmt)], "gridPos": pos,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background_gradient", "graphMode": graph,
                    "justifyMode": "auto", "orientation": "auto", "textMode": "auto"},
    }


def timeseries(title, targets, pos, unit="none", desc="", decimals=1, steps=None,
               stacked=False, legend="bottom", renames=None):
    custom = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10 if not stacked else 40,
              "showPoints": "never", "spanNulls": True, "axisPlacement": "auto"}
    defaults = {"unit": unit, "decimals": decimals, "color": {"mode": "palette-classic"},
                "custom": custom}
    if steps:
        defaults["thresholds"] = thresh(steps)
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "targets": targets, "gridPos": pos,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": legend, "showLegend": True,
                               "calcs": ["lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "transformations": renames or [],
    }


def bargauge(title, sql, pos, unit="bytes", desc="", renames=None):
    return {
        "type": "bargauge", "title": title, "description": desc, "datasource": DS,
        "targets": [target(sql)], "gridPos": pos,
        "fieldConfig": {"defaults": {"unit": unit, "decimals": 1,
                                     "color": {"mode": "palette-classic"}}, "overrides": []},
        "options": {"orientation": "horizontal", "displayMode": "gradient",
                    "showUnfilled": True, "minVizHeight": 16, "minVizWidth": 8,
                    "valueMode": "color",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "transformations": renames or [],
    }


def table(title, sql, pos, transforms=None, overrides=None, desc=""):
    return {
        "type": "table", "title": title, "description": desc, "datasource": DS,
        "targets": [target(sql, "table")], "gridPos": pos,
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True,
                                                "cellOptions": {"type": "auto"}}},
                        "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
        "transformations": transforms or [],
    }


def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "gridPos": grid(0, y, 24, 1),
            "panels": []}


def hide_time(rename=None):
    organize = {"id": "organize",
                "options": {"excludeByName": {"Time": True}, "renameByName": rename or {},
                            "indexByName": {}}}
    return [organize]


# ------------------------------------------------------------------- queries
Q_DISK_PCT = 'SELECT max("used_percent") AS "used" FROM "seaweedfs_disk" WHERE $timeFilter GROUP BY "path"'
Q_DISK_USED = 'SELECT max("used_bytes") AS "used" FROM "seaweedfs_disk" WHERE $timeFilter GROUP BY "path"'
Q_DISK_FREE = 'SELECT min("free_bytes") AS "free" FROM "seaweedfs_disk" WHERE $timeFilter GROUP BY "path"'
Q_SLOTS = 'SELECT max("free_volume_slots") AS "free_slots" FROM "seaweedfs_volume_server" WHERE $timeFilter GROUP BY "server"'
Q_TS_DISK_USED = ('SELECT max("used_bytes") AS "used" FROM "seaweedfs_disk" '
                  'WHERE $timeFilter GROUP BY time($__interval), "path" fill(linear)')
Q_TS_DISK_FREE = ('SELECT min("free_bytes") AS "free" FROM "seaweedfs_disk" '
                  'WHERE $timeFilter GROUP BY time($__interval), "path" fill(linear)')
Q_TS_DISK_PCT = ('SELECT max("used_percent") AS "used" FROM "seaweedfs_disk" '
                 'WHERE $timeFilter GROUP BY time($__interval), "path" fill(linear)')
Q_TS_SLOTS = ('SELECT max("free_volume_slots") AS "free_slots", max("volume_count") AS "used_slots" '
              'FROM "seaweedfs_volume_server" WHERE $timeFilter GROUP BY time($__interval), "server" fill(null)')
Q_TS_CLUSTER = ('SELECT max("volume_count") AS "volumes", max("max_volume_slots") AS "slots", '
                'max("free_volume_slots") AS "free_slots" FROM "seaweedfs_cluster" '
                'WHERE $timeFilter GROUP BY time($__interval) fill(linear)')
Q_TS_EC = ('SELECT max("ec_shard_count") AS "ec_shards", max("volume_layout_count") AS "layouts" '
           'FROM "seaweedfs_cluster" WHERE $timeFilter GROUP BY time($__interval) fill(linear)')
Q_BAR_SIZE = 'SELECT max("size_bytes") AS "size" FROM "seaweedfs_collection" WHERE $timeFilter GROUP BY "collection"'
Q_LEADER = ('SELECT count("is_leader") AS "leader_samples" FROM "seaweedfs_master" '
            'WHERE $timeFilter AND "is_leader" = true GROUP BY "server"')
Q_READONLY = ('SELECT sum("readonly_volume_count") AS "readonly" FROM (SELECT '
              'max("readonly_volume_count") AS "readonly_volume_count" FROM "seaweedfs_collection" '
              'WHERE $timeFilter GROUP BY "collection")')
Q_RECLAIM = ('SELECT sum("deleted_bytes") AS "reclaimable" FROM (SELECT '
             'max("deleted_bytes") AS "deleted_bytes" FROM "seaweedfs_collection" '
             'WHERE $timeFilter GROUP BY "collection")')
Q_MAXID = 'SELECT max("max_volume_id") AS "id" FROM "seaweedfs_master" WHERE $timeFilter'
Q_SCRAPERS = ('SELECT count("max_volume_id") AS "samples" FROM "seaweedfs_master" '
              'WHERE $timeFilter GROUP BY "host"')

Q_TOTAL_SIZE = ('SELECT sum("size_bytes") AS "total" FROM (SELECT max("size_bytes") AS "size_bytes" '
                'FROM "seaweedfs_collection" WHERE $timeFilter GROUP BY "collection")')
Q_TOTAL_FILES = ('SELECT sum("file_count") AS "total" FROM (SELECT max("file_count") AS "file_count" '
                 'FROM "seaweedfs_collection" WHERE $timeFilter GROUP BY "collection")')
Q_TOTAL_VOLUMES = ('SELECT sum("volume_count") AS "total" FROM (SELECT max("volume_count") AS "volume_count" '
                   'FROM "seaweedfs_collection" WHERE $timeFilter GROUP BY "collection")')
Q_TOTAL_WRITABLE = ('SELECT sum("writable_volume_count") AS "total" FROM (SELECT '
                    'max("writable_volume_count") AS "writable_volume_count" FROM "seaweedfs_collection" '
                    'WHERE $timeFilter GROUP BY "collection")')
Q_TS_COL_SIZE = ('SELECT max("size_bytes") AS "size" FROM "seaweedfs_collection" '
                 'WHERE $timeFilter GROUP BY time($__interval), "collection" fill(linear)')
Q_TS_COL_FILES = ('SELECT max("file_count") AS "files" FROM "seaweedfs_collection" '
                  'WHERE $timeFilter GROUP BY time($__interval), "collection" fill(linear)')
Q_TS_COL_VOLUMES = ('SELECT max("volume_count") AS "volumes" FROM "seaweedfs_collection" '
                    'WHERE $timeFilter GROUP BY time($__interval), "collection" fill(null)')
Q_TS_COL_WRITABLE = ('SELECT max("writable_volume_count") AS "writable" FROM "seaweedfs_collection" '
                     'WHERE $timeFilter GROUP BY time($__interval), "collection" fill(null)')
Q_GROWTH_BYTES = ('SELECT non_negative_difference(max("size_bytes")) AS "bytes_added" '
                  'FROM "seaweedfs_collection" WHERE $timeFilter '
                  'GROUP BY time($__interval), "collection"')
Q_GROWTH_FILES = ('SELECT non_negative_difference(max("file_count")) AS "files_added" '
                  'FROM "seaweedfs_collection" WHERE $timeFilter '
                  'GROUP BY time($__interval), "collection"')
Q_TABLE_COLLECTIONS = ('SELECT last("size_bytes") AS "size", last("file_count") AS "files", '
                       'last("volume_count") AS "volumes", last("writable_volume_count") AS "writable" '
                       'FROM "seaweedfs_collection" WHERE $timeFilter GROUP BY "collection"')

LEADER_MAP = [{"type": "value", "options": {
    "true": {"text": "Leader", "index": 0}, "false": {"text": "No leader", "index": 1}}}]


# --------------------------------------------------------------- dashboard 1
def cluster_overview():
    panels = [
        stat("Disk used", Q_DISK_PCT, grid(0, 0, 6, 5), unit="percent", decimals=1,
             steps=[("green", None), ("yellow", 70), ("red", 85)], mn=0, mx=100,
             desc="Highest used percentage per disk, across all scrapers."),
        stat("Disk used", Q_DISK_USED, grid(6, 0, 6, 5), unit="bytes",
             steps=[("blue", None)],
             desc="Bytes used on the SeaweedFS volume disks."),
        stat("Disk free", Q_DISK_FREE, grid(12, 0, 6, 5), unit="bytes",
             steps=[("red", None), ("yellow", 5e10), ("green", 1.5e11)],
             desc="Lowest free bytes across disks. Red below 50 GB, green above 150 GB."),
        stat("Volume slots free", Q_SLOTS, grid(18, 0, 6, 5), unit="none", decimals=0,
             steps=[("red", None), ("yellow", 1), ("green", 5)],
             desc="Unused volume slots of the least empty volume server. "
                  "At 0 the master cannot grow new volumes."),

        row("Capacity", 5),
        timeseries("Disk usage over time", [
            target(Q_TS_DISK_USED, ref="A"),
            target(Q_TS_DISK_FREE.replace("min(\"free_bytes\")", "min(\"free_bytes\")"), ref="B"),
        ], grid(0, 6, 12, 8), unit="bytes", renames=[PATH_RE],
            desc="Used and free bytes per volume disk."),
        timeseries("Disk used percent", [target(Q_TS_DISK_PCT)], grid(12, 6, 12, 8),
                   unit="percent", steps=[("green", None), ("yellow", 70), ("red", 85)],
                   renames=[PATH_RE],
                   desc="Sustained values above 80 % need attention: SeaweedFS stops "
                        "writing to a volume server at 95 % by default."),
        timeseries("Volume slots per server", [target(Q_TS_SLOTS)], grid(0, 14, 8, 7),
                   unit="none", renames=[SERVER_RE], legend="right",
                   desc="Used volumes and free volume slots per volume server."),
        timeseries("Cluster volumes and slots", [target(Q_TS_CLUSTER)], grid(8, 14, 8, 7),
                   unit="none", renames=[CLUSTER_RE], legend="right",
                   desc="Cluster-wide volumes, maximum slots and free slots."),
        bargauge("Stored bytes per collection", Q_BAR_SIZE, grid(16, 14, 8, 7),
                 renames=[COLLECTION_RE],
                 desc="Physical bytes per collection (replicas counted)."),

        row("Health", 21),
        stat("Leader samples", Q_LEADER, grid(0, 22, 6, 4), decimals=0, graph="none",
             steps=[("red", None), ("green", 1)],
             desc="Samples in which the master reported itself as leader. Must keep "
                  "growing; 0 means no leader is elected."),
        stat("Read-only volumes", Q_READONLY, grid(6, 22, 6, 4), decimals=0,
             steps=[("green", None), ("red", 1)], graph="none",
             desc="Read-only volumes reject writes. Should always be 0."),
        stat("Reclaimable bytes", Q_RECLAIM, grid(12, 22, 6, 4), unit="bytes",
             steps=[("green", None), ("yellow", 1e9)],
             desc="Deleted bytes not yet vacuumed — candidate for `weed shell volume.vacuum`. "
                  "The deleted file count per collection is in the Collection snapshot table."),
        stat("Highest volume ID", Q_MAXID, grid(18, 22, 6, 4), decimals=0, steps=[("blue", None)],
             desc="Last volume ID assigned by the master."),
        table("Scrapers reporting", Q_SCRAPERS, grid(0, 26, 12, 7),
              transforms=hide_time({"host": "Scraper", "samples": "Samples"}),
              overrides=[{"matcher": {"id": "byName", "options": "Samples"},
                          "properties": [
                              {"id": "custom.cellOptions",
                               "value": {"type": "gauge", "mode": "gradient"}},
                              {"id": "thresholds",
                               "value": {"mode": "absolute",
                                         "steps": [{"color": "green", "value": None}]}},
                              {"id": "color", "value": {"mode": "thresholds"}}]}],
              desc="Samples received per Telegraf host in the selected range. "
                   "A host with far fewer samples is stale."),
        timeseries("EC shards and volume layouts", [target(Q_TS_EC)], grid(12, 26, 12, 7),
                   unit="none", renames=[CLUSTER_RE], legend="right",
                   desc="Erasure-coded shards and number of volume layouts."),
    ]
    return {
        "uid": "seaweedfs-cluster",
        "title": "SeaweedFS — Cluster Overview",
        "tags": ["seaweedfs", "telegraf", "storage"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "schemaVersion": 39,
        "panels": panels,
    }


# --------------------------------------------------------------- dashboard 2
def collections_growth():
    panels = [
        stat("Total stored", Q_TOTAL_SIZE, grid(0, 0, 6, 5), unit="bytes", steps=[("blue", None)],
             desc="Sum of the physical size of all collections."),
        stat("Total files", Q_TOTAL_FILES, grid(6, 0, 6, 5), unit="short", decimals=1,
             steps=[("blue", None)], desc="Sum of the file counts of all collections."),
        stat("Volumes", Q_TOTAL_VOLUMES, grid(12, 0, 6, 5), decimals=0, steps=[("blue", None)],
             desc="Physical volumes across collections (each replica counts)."),
        stat("Writable volumes", Q_TOTAL_WRITABLE, grid(18, 0, 6, 5), decimals=0,
             steps=[("red", None), ("yellow", 1), ("green", 3)],
             desc="Volumes the master can still write to. Zero means the next write fails."),

        row("Per collection", 5),
        timeseries("Stored bytes per collection", [target(Q_TS_COL_SIZE)], grid(0, 6, 12, 8),
                   unit="bytes", renames=[COLLECTION_RE],
                   desc="Physical size per collection over time."),
        timeseries("Files per collection", [target(Q_TS_COL_FILES)], grid(12, 6, 12, 8),
                   unit="short", renames=[COLLECTION_RE],
                   desc="Object count per collection over time."),
        timeseries("Volumes per collection", [target(Q_TS_COL_VOLUMES)], grid(0, 14, 8, 7),
                   unit="none", renames=[COLLECTION_RE]),
        timeseries("Writable volumes per collection", [target(Q_TS_COL_WRITABLE)], grid(8, 14, 8, 7),
                   unit="none", renames=[COLLECTION_RE]),
        table("Collection snapshot", Q_TABLE_COLLECTIONS, grid(16, 14, 8, 7),
              transforms=hide_time({"collection": "Collection", "size": "Size",
                                    "files": "Files", "volumes": "Volumes",
                                    "writable": "Writable"}),
              overrides=[{"matcher": {"id": "byName", "options": "Size"},
                          "properties": [{"id": "unit", "value": "bytes"}]}],
              desc="Latest values per collection."),

        row("Growth", 21),
        timeseries("Bytes added per interval", [target(Q_GROWTH_BYTES)], grid(0, 22, 12, 8),
                   unit="bytes", renames=[COLLECTION_RE], desc="Non-negative difference of the collection size between "
                                      "buckets — the ingestion rate in bytes."),
        timeseries("Files added per interval", [target(Q_GROWTH_FILES)], grid(12, 22, 12, 8),
                   unit="short", renames=[COLLECTION_RE], desc="Files added per bucket."),
        {"type": "text", "title": "Reading these panels", "gridPos": grid(0, 30, 24, 4),
         "options": {"mode": "markdown", "code": {"showLineNumbers": False}, "content": (
             "**Volume counts are physical.** Every replica of a volume counts, so with "
             "replication a collection shows a multiple of its logical volume count.\n\n"
             "**Growth panels** use `non_negative_difference` of the per-bucket maximum, so a "
             "bucket shows the bytes/files added inside it. Volumes are allocated in "
             "30 GB chunks by default, which is why *Volumes per collection* moves in steps "
             "while *Stored bytes* grows smoothly.\n\n"
             "**Vacuum:** `Reclaimable bytes` above zero means deleted needles are still "
             "occupying disk — run `weed shell` → `volume.vacuum` during a quiet window."
         )},
         "fieldConfig": {"defaults": {}, "overrides": []},
         "targets": [], "description": "", "datasource": DS,
         "pluginVersion": "13.2.4"},
    ]
    return {
        "uid": "seaweedfs-collections",
        "title": "SeaweedFS — Collections & Growth",
        "tags": ["seaweedfs", "telegraf", "storage", "capacity"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "schemaVersion": 39,
        "panels": panels,
    }


# ------------------------------------------------------------------ main
def upsert(folder_uid, dashboard):
    # classic dashboards carry numeric panel ids; Grafana's schema migration is
    # happier with them present
    for i, panel in enumerate(dashboard["panels"], start=1):
        panel["id"] = i
    code, body = call("POST", "/api/dashboards/db", {
        "dashboard": dashboard, "folderUid": folder_uid, "overwrite": True,
        "message": "managed by seaweedfs-telegraf/grafana/build_dashboards.py"})
    if code != 200:
        raise SystemExit(f"dashboard {dashboard['uid']} failed: {code} {body}")
    print(f"dashboard {body.get('uid')} v{body.get('version')}: {body.get('url')}")
    return body


def check_overlaps(dashboard):
    taken = {}
    for p in dashboard["panels"]:
        g = p["gridPos"]
        for y in range(g["y"], g["y"] + g["h"]):
            for x in range(g["x"], g["x"] + g["w"]):
                if (x, y) in taken:
                    raise SystemExit(f"overlap: {p['title']} at {g} collides with {taken[(x, y)]}")
                taken[(x, y)] = p["title"]
    print(f"  layout ok: {len(dashboard['panels'])} panels, no overlaps")


VERIFY_SQL = [
    ("disk pct", Q_DISK_PCT), ("disk used", Q_DISK_USED), ("disk free", Q_DISK_FREE),
    ("slots", Q_SLOTS), ("ts disk used", Q_TS_DISK_USED), ("ts disk free", Q_TS_DISK_FREE),
    ("ts disk pct", Q_TS_DISK_PCT), ("ts slots", Q_TS_SLOTS), ("ts cluster", Q_TS_CLUSTER),
    ("ts ec", Q_TS_EC), ("bar size", Q_BAR_SIZE), ("leader", Q_LEADER),
    ("readonly", Q_READONLY), ("reclaim", Q_RECLAIM), ("max id", Q_MAXID),
    ("scrapers", Q_SCRAPERS), ("total size", Q_TOTAL_SIZE), ("total files", Q_TOTAL_FILES),
    ("total volumes", Q_TOTAL_VOLUMES), ("writable", Q_TOTAL_WRITABLE),
    ("ts col size", Q_TS_COL_SIZE), ("ts col files", Q_TS_COL_FILES),
    ("ts col volumes", Q_TS_COL_VOLUMES), ("ts col writable", Q_TS_COL_WRITABLE),
    ("growth bytes", Q_GROWTH_BYTES), ("growth files", Q_GROWTH_FILES),
    ("table collections", Q_TABLE_COLLECTIONS),
]


def verify():
    ok = fail = 0
    for label, sql in VERIFY_SQL:
        code, body = call("POST", "/api/ds/query", {
            "queries": [{"refId": "A", "datasource": DS, "query": sql, "rawQuery": True,
                         "resultFormat": "table", "intervalMs": 60000, "maxDataPoints": 500}],
            "from": "now-6h", "to": "now"})
        if code != 200:
            print(f"  FAIL {label}: {code} {str(body)[:160]}")
            fail += 1
            continue
        res = (body.get("results") or {}).get("A", {})
        if "error" in res:
            print(f"  FAIL {label}: {str(res['error'])[:160]}")
            fail += 1
            continue
        frames = res.get("frames", [])
        rows = len(frames[0]["data"]["values"][0]) if frames and frames[0]["data"]["values"] else 0
        if rows == 0:
            print(f"  WARN {label}: query returned no rows")
            ok += 1
            continue
        print(f"  ok   {label}: {len(frames)} series, {rows} rows")
        ok += 1
    print(f"verify: {ok} panels ok, {fail} failed")
    return fail == 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    folder = ensure_folder()
    print("folder:", folder)
    for build in (cluster_overview, collections_growth):
        dash = build()
        check_overlaps(dash)
        upsert(folder, dash)
    if args.verify:
        print("verifying panel queries:")
        raise SystemExit(0 if verify() else 1)
