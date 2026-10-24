#!/usr/bin/env python3
"""Report drift between the SeaweedFS release in the cluster and this repo.

Three independent comparisons:

  1. values      helm get values  vs  the values file on disk
  2. manifests   helm template -f values  vs  helm get manifest
  3. no-op       (implied by 1 and 2: if both match, the file reproduces the cluster)

Helm never stores hook resources in `get manifest`, and the chart hashes freshly
generated S3 credentials into a `checksum/` annotation on every render, so those two
classes of difference are normalised away before comparing. Anything else is drift —
a kubectl edit, a --set at upgrade time, or a stale file.

Usage:
  ./drift.py                       # compare with defaults below
  ./drift.py --values ../helm/x.yaml --version 4.48.0
  ./drift.py --verbose             # show a diff for each differing resource

Exit codes: 0 clean, 1 drift found, 2 could not run a comparison.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

DEFAULTS = {
    "release": "seaweedfs",
    "namespace": "s3",
    "chart": "seaweedfs/seaweedfs",
    "version": "4.48.0",
    "values": os.path.join(os.path.dirname(os.path.abspath(__file__)), "seaweedfs-values.yaml"),
}

# hook resources: executed at install/upgrade, never kept in `get manifest`
HOOK_SOURCES = ("s3-secret.yaml", "bucket-hook", "post-install", "pre-upgrade")

CHECKSUM_RE = re.compile(r"^(\s*checksum/[^:]*:).*$", re.M)


def helm(*args: str) -> str:
    env = dict(os.environ)
    try:
        return subprocess.run(["helm", *args], check=True, capture_output=True, text=True,
                              env=env).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"helm {' '.join(args)} failed:\n{e.stderr.strip()[:800]}")
    except FileNotFoundError:
        sys.exit("helm not found in PATH")


def split_manifest(text: str) -> dict[str, str]:
    """resource key (source + kind + name) -> normalised body, hooks removed."""
    out: dict[str, str] = {}
    for doc in text.split("\n---\n"):
        if not doc.strip():
            continue
        src = re.search(r"# Source: (\S+)", doc)
        src = src.group(1) if src else "?"
        if any(h in src for h in HOOK_SOURCES):
            continue
        kind = re.search(r"^kind:\s*(\S+)", doc, re.M)
        name = re.search(r"^  name:\s*(\S+)", doc, re.M)
        key = f"{src}  [{kind.group(1) if kind else '?'} {name.group(1) if name else '?'}]"
        out[key] = CHECKSUM_RE.sub(r"\1 <generated>", doc.strip())
    return out


def compare_values(values_file: str, release: str, namespace: str) -> bool:
    live = json.loads(helm("-n", namespace, "get", "values", release, "-o", "json") or "{}")
    import yaml
    with open(values_file) as f:
        want = yaml.safe_load(f) or {}

    def flatten(d, prefix=""):
        flat = {}
        if isinstance(d, dict):
            for k, v in d.items():
                flat.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        elif isinstance(d, list):
            flat[prefix] = json.dumps(d, sort_keys=True)
        else:
            flat[prefix] = d
        return flat

    lv, wv = flatten(live), flatten(want)
    only_live = {k: lv[k] for k in lv.keys() - wv.keys()}
    only_file = {k: wv[k] for k in wv.keys() - lv.keys()}
    differs = {k: (lv[k], wv[k]) for k in lv.keys() & wv.keys() if lv[k] != wv[k]}
    if not (only_live or only_file or differs):
        print(f"  values      : match ({len(wv)} settings)")
        return True
    print("  values      : DRIFT")
    for k, v in list(only_live.items())[:10]:
        print(f"      in cluster only : {k} = {v}")
    for k, v in list(only_file.items())[:10]:
        print(f"      in file only    : {k} = {v}")
    for k, (a, b) in list(differs.items())[:10]:
        print(f"      differs         : {k}: cluster={a} file={b}")
    return False


def compare_manifests(values_file: str, release: str, namespace: str, chart: str,
                      version: str, verbose: bool) -> bool:
    live = split_manifest(helm("-n", namespace, "get", "manifest", release))
    rendered = split_manifest(
        helm("-n", namespace, "template", release, chart, "--version", version,
             "-f", values_file))
    missing = {k: v for k, v in rendered.items() if k not in live}
    extra = {k: v for k, v in live.items() if k not in rendered}
    changed = {k: (live[k], rendered[k]) for k in live.keys() & rendered.keys()
               if live[k] != rendered[k]}
    if not (missing or extra or changed):
        print(f"  manifests   : match ({len(live)} resources, hooks and checksums ignored)")
        return True
    print(f"  manifests   : DRIFT ({len(missing)} missing, {len(extra)} extra, {len(changed)} changed)")
    for k in list(missing)[:8]:
        print(f"      not deployed    : {k}")
    for k in list(extra)[:8]:
        print(f"      not in the file : {k}")
    for k, (a, b) in list(changed.items())[:8]:
        print(f"      changed         : {k}")
        if verbose:
            import difflib
            diff = list(difflib.unified_diff(a.splitlines(), b.splitlines(),
                                             "cluster", "file", lineterm="", n=1))
            for line in diff[:24]:
                print(f"        {line}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", default=DEFAULTS["release"])
    ap.add_argument("--namespace", default=DEFAULTS["namespace"])
    ap.add_argument("--chart", default=DEFAULTS["chart"])
    ap.add_argument("--version", default=DEFAULTS["version"])
    ap.add_argument("--values", default=DEFAULTS["values"])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.values):
        sys.exit(f"values file not found: {args.values}")
    print(f"drift check: {args.namespace}/{args.release} vs {os.path.basename(args.values)} "
          f"(chart {args.chart} {args.version})")
    ok_values = compare_values(args.values, args.release, args.namespace)
    ok_manifests = compare_manifests(args.values, args.release, args.namespace,
                                     args.chart, args.version, args.verbose)
    if ok_values and ok_manifests:
        print("result      : no drift — the values file reproduces the running cluster")
        return 0
    print("result      : drift found — reconcile before the next upgrade "
          "(helm upgrade with the pinned version, not kubectl edit)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
