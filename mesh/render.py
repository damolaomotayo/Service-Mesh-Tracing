#!/usr/bin/env python3
"""The mesh's "control plane" in one file: reads mesh.yaml + traffic.yaml and writes each pod's sidecar config to
mesh/generated/<pod>/ (traefik.yml and dynamic/mesh.yml). The sidecars watch their dynamic/ folder, so changing
traffic.yaml and re-running this script re-routes live traffic with no restart.

  render.py            write all sidecar configs
  render.py pods       print the pod names (used by certs.sh)
"""
import os
import sys

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CERTS = "/certs"


def load(root=ROOT):
    with open(os.path.join(root, "mesh", "mesh.yaml"), encoding="utf-8") as fh:
        mesh = yaml.safe_load(fh)
    with open(os.path.join(root, "mesh", "traffic.yaml"), encoding="utf-8") as fh:
        traffic = yaml.safe_load(fh)
    return mesh, traffic


def validate(mesh, traffic):
    pods = mesh["pods"]
    services = {p["service"] for p in pods.values()}
    for name, p in pods.items():
        if p.get("upstream") and p["upstream"] not in services:
            raise ValueError(f"{name}: upstream '{p['upstream']}' is not a service in the mesh")
    for svc, rules in traffic.items():
        if svc == "retry_attempts":
            if not isinstance(rules, int) or rules < 1:
                raise ValueError("retry_attempts must be a whole number, 1 or more")
            continue
        versions = {p.get("version") for p in pods.values() if p["service"] == svc}
        weights = rules.get("weights", {})
        unknown = set(weights) - versions
        if unknown:
            raise ValueError(f"{svc}: weights for unknown versions {sorted(unknown)}")
        if weights and sum(int(w) for w in weights.values()) <= 0:
            raise ValueError(f"{svc}: at least one version needs a weight above 0")
        hr = rules.get("header_route")
        if hr and hr["version"] not in versions:
            raise ValueError(f"{svc}: header_route points at unknown version {hr['version']}")


def sidecar_config(pod, mesh, traffic, net=None):
    """Returns (static_config, dynamic_config) for one pod. `net` lets tests run the mesh on one machine:
    net = {"ip": {pod: "127.0.10.N"}, "tracing": url, "certs": dir, "dynamic_dir": "/x/{pod}/dynamic"}; by default addresses are the Docker ones."""
    ports, pods, policy = mesh["ports"], mesh["pods"], mesh["policy"]
    me = pods[pod]
    ip = (net or {}).get("ip", {})
    mine = ip.get(pod)
    app_host = mine or "127.0.0.1"
    bind = mine or ""
    peer = lambda name: ip.get(name) or name    # noqa: E731  (a pod's address as seen by its peers)
    endpoint = (net or {}).get("tracing") or mesh["tracing"]["endpoint"]

    entry = {"inbound": {"address": f"{bind}:{ports['inbound']}"}}
    if me.get("upstream"):
        entry["outbound"] = {"address": f"{app_host}:{ports['outbound']}"}
    if me.get("gateway"):
        entry["gateway"] = {"address": f"{bind}:{ports['gateway']}"}
    static = {
        "global": {"checkNewVersion": False, "sendAnonymousUsage": False},
        "log": {"level": "INFO"},
        "entryPoints": entry,
        "providers": {"file": {"directory": ((net or {}).get("dynamic_dir") or "/etc/traefik/dynamic").format(pod=pod), "watch": True}},
        "tracing": {"serviceName": f"{pod}-sidecar", "otlp": {"http": {"endpoint": endpoint}}},
    }

    http = {"routers": {}, "services": {}, "middlewares": {}, "serversTransports": {}}
    certs = (net or {}).get("certs", CERTS)
    http["services"]["local-app"] = {"loadBalancer": {"servers": [{"url": f"http://{app_host}:{ports['app']}"}]}}
    # in: only clients holding a certificate signed by the mesh CA get through
    http["routers"]["in"] = {"rule": "PathPrefix(`/`)", "entryPoints": ["inbound"], "service": "local-app", "tls": {}}
    if me.get("gateway"):
        http["routers"]["gateway"] = {"rule": "PathPrefix(`/`)", "entryPoints": ["gateway"], "service": "local-app"}

    tls = {
        "certificates": [{"certFile": f"{certs}/{pod}.pem", "keyFile": f"{certs}/{pod}.key"}],
        "options": {"default": {"minVersion": "VersionTLS12",
                                "clientAuth": {"caFiles": [f"{certs}/ca.pem"], "clientAuthType": "RequireAndVerifyClientCert"}}},
    }

    up = me.get("upstream")
    if up:
        targets = {n: p for n, p in pods.items() if p["service"] == up}
        rules = traffic.get(up, {})
        for n in targets:
            http["serversTransports"][f"to-{n}"] = {
                "serverName": n, "rootCAs": [f"{certs}/ca.pem"],
                "certificates": [{"certFile": f"{certs}/{pod}.pem", "keyFile": f"{certs}/{pod}.key"}],
                "forwardingTimeouts": {"responseHeaderTimeout": policy["timeouts"][up]}}
            http["services"][n] = {"loadBalancer": {"serversTransport": f"to-{n}",
                                                    "servers": [{"url": f"https://{peer(n)}:{ports['inbound']}"}]}}
        weights = rules.get("weights") or {}
        if len(targets) == 1:
            main = next(iter(targets))
        else:
            live = [{"name": n, "weight": int(weights.get(p.get("version"), 0))} for n, p in targets.items()]
            live = [w for w in live if w["weight"] > 0]
            http["services"][up] = {"weighted": {"services": live}}
            main = up
        http["middlewares"]["retry"] = {"retry": {"attempts": int(traffic.get("retry_attempts", 3)), "initialInterval": "50ms"}}
        http["middlewares"]["breaker"] = {"circuitBreaker": {"expression": policy["breaker"], "checkPeriod": "200ms",
                                                              "fallbackDuration": "3s", "recoveryDuration": "2s"}}
        chain = ["breaker", "retry"]      # the breaker sits OUTSIDE retry: it judges what users get after retries, not each failed attempt
        hr = rules.get("header_route")
        if hr and len(targets) > 1:
            target = next(n for n, p in targets.items() if p.get("version") == hr["version"])
            http["routers"][f"to-{up}-by-header"] = {
                "rule": f"PathPrefix(`/`) && Header(`{hr['header']}`, `{hr['value']}`)", "priority": 100,
                "entryPoints": ["outbound"], "service": target, "middlewares": list(chain)}
        http["routers"][f"to-{up}"] = {"rule": "PathPrefix(`/`)", "entryPoints": ["outbound"], "service": main, "middlewares": list(chain)}

    dynamic = {"http": {k: v for k, v in http.items() if v}, "tls": tls}
    return static, dynamic


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def render(root=ROOT, net=None, out=None):
    mesh, traffic = load(root)
    validate(mesh, traffic)
    out = out or os.path.join(root, "mesh", "generated")
    banner = "# GENERATED by mesh/render.py. Do not edit; edit mesh.yaml or traffic.yaml instead.\n"
    for pod in mesh["pods"]:
        static, dynamic = sidecar_config(pod, mesh, traffic, net)
        write_atomic(os.path.join(out, pod, "traefik.yml"), banner + yaml.safe_dump(static, sort_keys=False))
        write_atomic(os.path.join(out, pod, "dynamic", "mesh.yml"), banner + yaml.safe_dump(dynamic, sort_keys=False))
    return list(mesh["pods"])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pods":
        print("\n".join(load()[0]["pods"]))
    else:
        try:
            names = render()
        except (ValueError, KeyError, OSError, yaml.YAMLError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"rendered sidecar configs for: {', '.join(names)}")
