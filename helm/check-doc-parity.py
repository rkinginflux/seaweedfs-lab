#!/usr/bin/env python3
"""Check that the lab's install sequence and the repo's rebuild checklist agree.

The lab keeps `~/lab/SEQUENCE-FOR-INSTALL.md` with real paths, private bootstrap scripts
and private wiring (Telegram, LAN addresses). The repo ships `docs/rebuild-checklist.md`
with repo paths and generic guidance. Both must describe the same *steps*.

This compares the mutating steps only — Helm releases, database creation and retention,
the provisioning scripts, applies and patches — after mapping the lab's filenames onto
the repo's. Read-only checks are deliberately not compared: both documents verify the
same things but phrase them differently, and that is not drift.

    ./check-doc-parity.py
    ./check-doc-parity.py --lab ~/lab/SEQUENCE-FOR-INSTALL.md --repo ../docs/rebuild-checklist.md

Exit codes: 0 in sync, 1 drift, 2 a file is missing.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

REPO_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs",
                            "rebuild-checklist.md")
LAB_DEFAULT = os.path.expanduser("~/lab/SEQUENCE-FOR-INSTALL.md")

# lab filename -> the repo's generic filename
FILE_MAP = {
    "~/lab/telegraf.yaml": "helm/telegraf-values.yaml",
    "~/lab/grafana/values.yaml": "helm/grafana-values.yaml",
    "~/lab/influxdb3-enterprise-values.yaml": "helm/influxdb3-enterprise-values.yaml",
    "~/lab/seaweedfs-values.yaml": "helm/seaweedfs-values.yaml",
    "~/lab/seaweedfs-ui-proxy.yaml": "helm/seaweedfs-ui-proxy.yaml",
    "~/lab/patch-db3-webui.sh": "helm/patch-db3-processor.sh",
}
FILE_MAP.update({k.replace("~/lab/", "$HOME/lab/"): v for k, v in list(FILE_MAP.items())})

# a shell-block line is a step if it matches one of these; everything else is a check
STEP_PATTERNS = [
    r"^helm (upgrade|install)\b",
    r"^influxdb3 (create|update) database\b",
    r"^python3 (grafana/)?(build_|create_slots_alert)",
    r"^kubectl .*\b(apply|patch|delete pod)\b",
    r"^kubectl create namespace\b",
    r"^make\b",
    r"^(bash|\./) .*(bootstrap|patch-db3)",
]


def steps(text: str) -> list[str]:
    found: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        s = raw.strip()
        if not s or s.startswith(("#", "|")):
            continue
        s = re.sub(r"\s+#.*$", "", s)
        for a, b in FILE_MAP.items():
            s = s.replace(a, b)
        s = (s.replace("./helm/", "helm/").replace("<repo>/helm", "helm")
              .replace("<repo>/grafana", "grafana")
              .replace("python3 build_", "python3 grafana/build_")
              .replace("python3 create_slots_alert", "python3 grafana/create_slots_alert")
              .replace('"', ""))
        s = re.sub(r"\s+", " ", s).strip()
        if any(re.search(p, s) for p in STEP_PATTERNS):
            found.append(s)
    return found


def sections(text: str) -> list[str]:
    return re.findall(r"^#{2,3}\s+(\d+(?:\.\d+)?)\.", text, re.M)


# equivalent spellings: the lab creates namespaces (and secrets) from its private
# bootstrap scripts, the repo does the namespace inline and the secrets by hand
EQUIVALENT_KEYS = {
    "bash:seaweedfs": "namespace:s3",
    "bash:db3": "namespace:influxdb3",
}


def key(step: str) -> str:
    """Coarse identity of a step: verb + the component it touches."""
    if "influxdb3 create database" in step or "influxdb3 update database" in step:
        return "database:retention"
    m = re.search(r"create namespace (\S+)", step)
    if m:
        return f"namespace:{m.group(1)}"
    for target in ("seaweedfs", "db3", "telegraf", "grafana"):
        if target in step:
            verb = step.split()[0]
            if "build_all_measurements" in step:
                return "python:all-measurements"
            if "build_dashboards" in step:
                return "python:dashboards"
            if "create_slots_alert" in step:
                return "python:alert"
            if "ui-proxy" in step:
                return "apply:ui-proxy"
            if "patch" in step:
                return "patch:processor"
            return f"{verb}:{target}"
    return step[:40]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default=LAB_DEFAULT)
    ap.add_argument("--repo", default=REPO_DEFAULT)
    args = ap.parse_args()

    for p in (args.lab, args.repo):
        if not os.path.exists(p):
            if p == args.lab:
                print(f"skipped: no lab sequence at {p}", file=sys.stderr)
                print("  (this file is private to the lab; pass --lab <path> to compare "
                      "another copy)", file=sys.stderr)
            else:
                print(f"missing file: {p}", file=sys.stderr)
            return 2

    lab, repo = open(args.lab).read(), open(args.repo).read()
    lab_steps, repo_steps = steps(lab), steps(repo)

    print(f"lab : {len(lab_steps)} steps, sections {','.join(sorted(set(sections(lab))))}")
    print(f"repo: {len(repo_steps)} steps, sections {','.join(sorted(set(sections(repo))))}")

    def keyset(steps_list):
        return {EQUIVALENT_KEYS.get(key(s), key(s)) for s in steps_list}

    lab_keys = keyset(lab_steps)
    repo_keys = keyset(repo_steps)
    drift = False

    missing = sorted(repo_keys - lab_keys)
    extra = sorted(lab_keys - repo_keys)
    if missing:
        drift = True
        print("\nrepo steps with no counterpart in the lab sequence:")
        for k in missing:
            print(f"  - {k}")
            for s in repo_steps:
                if key(s) == k:
                    print(f"      {s[:112]}")
    if extra:
        drift = True
        print("\nlab steps the repo checklist does not describe:")
        for k in extra:
            print(f"  + {k}")
            for s in lab_steps:
                if key(s) == k:
                    print(f"      {s[:112]}")

    sl, sr = set(sections(lab)), set(sections(repo))
    if sl != sr:
        drift = True
        print(f"\nsection numbers only in lab: {sorted(sl - sr) or '-'} | "
              f"only in repo: {sorted(sr - sl) or '-'}")

    if drift:
        print("\nresult: DRIFT — reconcile, then re-run")
        return 1
    print("\nresult: in sync — same steps after filename mapping, same section numbering")
    return 0


if __name__ == "__main__":
    sys.exit(main())
