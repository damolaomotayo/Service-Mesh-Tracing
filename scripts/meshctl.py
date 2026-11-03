#!/usr/bin/env python3
"""Small helper for the demo scripts (standard library plus PyYAML).

  meshctl.py status                      show the current traffic rules
  meshctl.py weights V1 V2               canary split in percent, e.g. 90 10 (applied live)
  meshctl.py retries N                   attempts per request, 1 = no retries (applied live)
  meshctl.py sample N [-H name:value]    send N requests to the public gateway; print e.g. "v1=60 v2=40 failed=0"
  meshctl.py trace                       find the newest trace in Jaeger; print its services; exit 0 if 3 or more
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "mesh"))
import render  # noqa: E402

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080")
JAEGER = os.environ.get("JAEGER_URL", "http://127.0.0.1:16686")
HEADER = "# Traffic rules. Edit by hand or use ./scripts/mesh.sh. Every change is applied live (no restarts).\n"


def traffic_path(root):
    return os.path.join(root, "mesh", "traffic.yaml")


def load_traffic(root=ROOT):
    with open(traffic_path(root), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def save_traffic(traffic, root=ROOT):
    mesh = render.load(root)[0]
    render.validate(mesh, traffic)                      # never write a file the renderer would refuse
    with open(traffic_path(root), "w", encoding="utf-8") as fh:
        fh.write(HEADER + yaml.safe_dump(traffic, sort_keys=False))
    render.render(root)


def set_weights(v1, v2, root=ROOT):
    if v1 < 0 or v2 < 0 or v1 + v2 <= 0:
        raise ValueError("weights must be 0 or more, and not both 0")
    t = load_traffic(root)
    t["catalog"]["weights"] = {"v1": v1, "v2": v2}
    save_traffic(t, root)


def set_retries(n, root=ROOT):
    t = load_traffic(root)
    t["retry_attempts"] = n
    save_traffic(t, root)


def sample(n, headers=None, base=GATEWAY):
    """Returns {"v1": 60, "v2": 40, "failed": 0}."""
    counts = {"failed": 0}
    for _ in range(n):
        req = urllib.request.Request(base + "/api/products", headers=headers or {})
        version = None
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                version = json.loads(resp.read())["catalog"]["version"]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            pass
        if version:
            counts[version] = counts.get(version, 0) + 1
        else:
            counts["failed"] += 1
    return counts


def trace_services(data):
    """From a Jaeger /api/traces reply: the trace with the most distinct services -> (trace_id, sorted services, span count)."""
    best = (None, [], 0)
    for tr in data.get("data") or []:
        names = sorted({p["serviceName"] for p in tr.get("processes", {}).values()})
        if len(names) > len(best[1]):
            best = (tr.get("traceID"), names, len(tr.get("spans", [])))
    return best


def find_trace(base=JAEGER, wait=25):
    end = time.time() + wait
    result = (None, [], 0)
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{base}/api/traces?service=frontend-sidecar&limit=5&lookback=1h", timeout=5) as resp:
                result = trace_services(json.loads(resp.read()))
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if len(result[1]) >= 3:
            break
        time.sleep(2)
    return result


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "status":
            print(HEADER + yaml.safe_dump(load_traffic(), sort_keys=False), end="")
        elif cmd == "weights" and len(argv) == 4:
            set_weights(int(argv[2]), int(argv[3]))
            print(f"catalog split is now v1={argv[2]}% v2={argv[3]}%")
        elif cmd == "retries" and len(argv) == 3:
            set_retries(int(argv[2]))
            print(f"retry attempts are now {argv[2]}")
        elif cmd == "sample" and len(argv) >= 3:
            headers = {}
            if "-H" in argv:
                name, _, value = argv[argv.index("-H") + 1].partition(":")
                headers[name.strip()] = value.strip()
            c = sample(int(argv[2]), headers)
            print(" ".join(f"{k}={v}" for k, v in sorted(c.items())))
        elif cmd == "trace":
            tid, names, spans = find_trace()
            print(f"trace {tid}: {spans} spans across {len(names)} services: {', '.join(names)}")
            return 0 if len(names) >= 3 else 1
        else:
            print(__doc__, file=sys.stderr)
            return 64
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
