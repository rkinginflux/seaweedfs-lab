#!/usr/bin/env python3
"""Create the SeaweedFS capacity alert (free volume slots < 5) in Grafana.

  * contact point   : existing Hermes Telegram bot (token read from ~/.hermes/.env,
                      never printed)
  * notification    : default policy routes to it
  * alert rule      : folder "SeaweedFS", group seaweedfs-capacity, eval 1m, for 5m

Idempotent: re-running updates the contact point, rule and policy in place.

Usage:
  kubectl -n grafana port-forward svc/grafana 3000:80 &
  python3 create_slots_alert.py [--test-contact-point]
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

GRAFANA = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
KUBECONFIG = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
DS_UID = os.environ.get("DS_UID", "bg24j3hn03cw0d")
FOLDER_TITLE = "SeaweedFS"
CP_NAME = "telegram-rick"
RULE_TITLE = "SeaweedFS volume slots low"
RULE_GROUP = "seaweedfs-capacity"
THRESHOLD = 5


def kubectl_secret(namespace: str, name: str, key: str) -> str:
    raw = subprocess.check_output(
        ["kubectl", "-n", namespace, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        env={**os.environ, "KUBECONFIG": KUBECONFIG})
    return base64.b64decode(raw).decode().strip()


def hermes_telegram() -> tuple[str, str]:
    env = {}
    path = os.path.expanduser("~/.hermes/.env")
    for line in open(path):
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_HOME_CHANNEL"]


USER = os.environ.get("GRAFANA_USER") or kubectl_secret("grafana", "grafana", "admin-user")
PASS = os.environ.get("GRAFANA_PASS") or kubectl_secret("grafana", "grafana", "admin-password")
AUTH = base64.b64encode(f"{USER}:{PASS}".encode()).decode()


def api(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(GRAFANA + path, data=data, method=method,
                                 headers={"Authorization": "Basic " + AUTH,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def folder_uid() -> str:
    code, folders = api("GET", "/api/folders")
    for f in folders or []:
        if f["title"] == FOLDER_TITLE:
            return f["uid"]
    code, res = api("POST", "/api/folders", {"title": FOLDER_TITLE})
    if code not in (200, 409):
        sys.exit(f"cannot create folder {FOLDER_TITLE}: {code} {res}")
    return res["uid"]


def ensure_contact_point(token: str, chat_id: str) -> str:
    """Returns the contact point uid."""
    cp = {"name": CP_NAME, "type": "telegram", "disableResolveMessage": False,
          "settings": {"bottoken": token, "chatid": chat_id}}
    code, existing = api("GET", "/api/v1/provisioning/contact-points")
    if code != 200:
        sys.exit(f"cannot list contact points: {code} {existing}")
    for c in existing:
        if c["name"] == CP_NAME:
            code, res = api("PUT", f"/api/v1/provisioning/contact-points/{c['uid']}", cp)
            if code not in (200, 202):
                sys.exit(f"cannot update contact point: {code} {res}")
            print(f"contact point          : updated {CP_NAME} ({res.get('uid')})")
            return res["uid"]
    code, res = api("POST", "/api/v1/provisioning/contact-points", cp)
    if code not in (200, 201, 202):
        sys.exit(f"cannot create contact point: {code} {res}")
    print(f"contact point          : created {CP_NAME} ({res.get('uid')})")
    return res["uid"]


def ensure_rule(folder: str):
    rule = {
        "title": RULE_TITLE,
        "ruleGroup": RULE_GROUP,
        "folderUID": folder,
        "orgID": 1,
        "condition": "C",
        "for": "5m",
        "noDataState": "NoData",
        "execErrState": "Error",
        "isPaused": False,
        "labels": {"severity": "warning", "component": "seaweedfs",
                   "signal": "capacity"},
        "annotations": {
            "summary": "SeaweedFS has fewer than {{ $values.B }} free volume slots",
            "description": (
                "free_volume_slots has been below %d for 5 minutes. When it reaches 0 "
                "the filer returns 500 'No writable volumes and no free volumes left' "
                "and S3 PUTs fail.\n\n"
                "Fix: raise master.maxVolumes in the seaweedfs helm values (each volume "
                "is 30 GB, so 11 slots need ~330 GB free on /data1), then "
                "helm upgrade seaweedfs --atomic --wait.\n\n"
                "Runbook: https://github.com/rkinginflux/seaweedfs-lab/blob/main/docs/runbook.md"
            ) % THRESHOLD,
        },
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 900, "to": 0},
                "datasourceUid": DS_UID,
                "model": {
                    "refId": "A", "hide": False,
                    "datasource": {"type": "influxdb", "uid": DS_UID},
                    "query": ('SELECT max("free_volume_slots") AS "free" '
                              'FROM "seaweedfs_cluster" WHERE time > now() - 15m '
                              'GROUP BY time(1m) fill(previous)'),
                    "rawQuery": True, "resultFormat": "time_series",
                    "intervalMs": 60000, "maxDataPoints": 100, "policy": "default",
                },
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 900, "to": 0},
                "datasourceUid": "__expr__",
                "model": {"refId": "B", "type": "reduce", "expression": "A",
                          "reducer": "last",
                          "datasource": {"type": "__expr__", "uid": "__expr__"},
                          "settings": {"mode": "dropNN"}},
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 900, "to": 0},
                "datasourceUid": "__expr__",
                "model": {"refId": "C", "type": "threshold", "expression": "B",
                          "datasource": {"type": "__expr__", "uid": "__expr__"},
                          "conditions": [{
                              "evaluator": {"params": [THRESHOLD], "type": "lt"},
                              "operator": {"type": "and"},
                              "query": {"params": ["C"]},
                              "reducer": {"type": "last"},
                              "type": "query"}]},
            },
        ],
    }
    code, existing = api("GET", "/api/v1/provisioning/alert-rules")
    if code != 200:
        sys.exit(f"cannot list rules: {code} {existing}")
    for r in existing:
        if r.get("title") == RULE_TITLE:
            rule["uid"] = r["uid"]
            code, res = api("PUT", f"/api/v1/provisioning/alert-rules/{r['uid']}", rule)
            if code not in (200, 201, 202):
                sys.exit(f"cannot update rule: {code} {res}")
            print(f"alert rule             : updated {RULE_TITLE} ({r['uid']})")
            return r["uid"]
    code, res = api("POST", "/api/v1/provisioning/alert-rules", rule)
    if code not in (200, 201, 202):
        sys.exit(f"cannot create rule: {code} {res}")
    print(f"alert rule             : created {RULE_TITLE} ({res.get('uid')})")
    return res["uid"]


def ensure_policy(cp_name: str):
    """Default route -> telegram contact point, keeping any existing routes."""
    code, cur = api("GET", "/api/v1/provisioning/policies")
    if code != 200:
        sys.exit(f"cannot read policy: {code} {cur}")
    policy = {
        "receiver": cp_name,
        "group_by": ["grafana_folder", "alertname"],
        "group_wait": "30s",
        "group_interval": "5m",
        "repeat_interval": "12h",
        "routes": cur.get("routes") or [],
    }
    code, res = api("PUT", "/api/v1/provisioning/policies", policy)
    if code not in (200, 202):
        sys.exit(f"cannot update policy: {code} {res}")
    print(f"notification policy    : default receiver -> {cp_name} "
          f"(repeat every 12h, group wait 30s)")


def verify_telegram(token: str, chat_id: str):
    """Grafana 13.2.4 removed /api/v1/provisioning/contact-points/test and its
    replacement rejects telegram receivers ("unknown integration type"), so verify
    the credentials the contact point uses straight from the Bot API instead. The
    rule firing end-to-end is the real delivery test."""
    def bot(method, payload=None):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(payload).encode() if payload else None,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    code, me = bot("getMe")
    print(f"telegram bot           : {code} @{(me.get('result') or {}).get('username')}")
    code, chat = bot("getChat", {"chat_id": chat_id})
    res = (chat.get("result") or {})
    ok = code == 200 and chat.get("ok")
    print(f"telegram chat          : {code} {'ok' if ok else chat.get('description')} "
          f"({res.get('type')} {res.get('title') or res.get('first_name') or ''})")
    if not ok:
        sys.exit("telegram credentials invalid — alert rules would not notify")


def rule_state(uid: str):
    code, res = api("GET", "/api/prometheus/grafana/api/v1/rules")
    if code != 200:
        return f"  (cannot read rule state: {code})"
    out = []
    for group in (res.get("data") or {}).get("groups") or []:
        for r in group.get("rules") or []:
            if r.get("name") == RULE_TITLE:
                al = r.get("alerts") or []
                vals = [a.get("value") for a in al]
                out.append(f"  state={r.get('state')} health={r.get('health')} "
                           f"value={vals} lastEval={r.get('lastEvaluation')}")
    return "\n".join(out) or "  (rule not evaluated yet)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-contact-point", action="store_true")
    args = ap.parse_args()

    token, chat_id = hermes_telegram()
    print(f"telegram target        : chat {chat_id[:4]}… token {len(token)} chars (from ~/.hermes/.env)")
    folder = folder_uid()
    print(f"folder                 : {FOLDER_TITLE} ({folder})")
    ensure_contact_point(token, chat_id)
    if args.test_contact_point:
        verify_telegram(token, chat_id)
    uid = ensure_rule(folder)
    ensure_policy(CP_NAME)
    print(f"rule uid               : {uid}")
    print("rule state (grafana will evaluate within ~1 min):")
    print(rule_state(uid))


if __name__ == "__main__":
    main()
