#!/usr/bin/env python3
"""Print SeaweedFS volume-slot headroom from the master's /dir/status JSON.

    kubectl -n s3 exec seaweedfs-master-0 -- wget -qO- http://127.0.0.1:9333/dir/status \
      | ./slots.py

Exits 1 when there are no free slots, because that is the state in which S3 writes
fail (see docs/storage-growth-and-retention.md).
"""
import json
import sys


def main() -> int:
    try:
        topo = json.load(sys.stdin)["Topology"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  cannot parse /dir/status: {e}", file=sys.stderr)
        return 2

    total, free = topo.get("Max", 0), topo.get("Free", 0)
    print(f"  slots: {total - free}/{total} used, {free} free")
    for layout in topo.get("Layouts") or []:
        name = layout.get("collection") or "(default)"
        writables = layout.get("writables") or []
        print(f"  {name:<12} writables={len(writables)}"
              + (f" ids={sorted(writables)}" if len(writables) <= 8 else ""))
    if free == 0:
        print("  WARNING: no free volume slots — the master cannot grow a volume and "
              "S3 writes will fail")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
