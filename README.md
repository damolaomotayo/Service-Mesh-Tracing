# Service Mesh and Tracing

A small service mesh on one EC2 instance, with only Docker installed and no other AWS services. Four "pods" (a frontend, catalog v1, catalog v2 and a pricing service) each run next to a **sidecar proxy** that handles encryption, routing, retries, timeouts and tracing. One request shows up as one trace in Jaeger.

The sidecar is **Traefik** and the rules live in a file, so this is the service-mesh *pattern* on a small footprint, not Istio or Linkerd (those need Kubernetes and add an automatic control plane). Here the "control plane" is `mesh/render.py`, which turns two small YAML files into each sidecar's config.

```
Browser :8080 --> [frontend sidecar] --mTLS--> [catalog-v1 sidecar] --mTLS--> [pricing sidecar]
                        |   90% / 10% canary   [catalog-v2 sidecar] --mTLS-->        |
                  [frontend app]               (each sidecar has its app beside it) |
every sidecar --OTLP spans--> Jaeger :16686
```

## What is in it

| Part | What it shows |
|---|---|
| `app/service.py` | One tiny stdlib-only Python service used for all roles. It speaks plain HTTP to its own sidecar and copies the trace and routing headers along |
| `docker-compose.yml` | 4 pods = app container + sidecar container sharing one network (`network_mode: service:`), plus Jaeger. 9 containers |
| `mesh/mesh.yaml` | Topology and policy: who calls whom, ports, timeouts, circuit breaker rule |
| `mesh/traffic.yaml` | Live rules: canary weights, header route, retry attempts |
| `mesh/render.py` | The control plane in one file: writes every sidecar's config. Sidecars reload it live |
| `scripts/certs.sh` | Makes the mesh's own certificate authority and one certificate per pod (mTLS) |
| `scripts/mesh.sh`, `scripts/chaos.sh` | Change traffic rules live; make a service slow or failing |
| `scripts/` | `setup`, `preflight`, `up`, `smoke`, `demo`, `destroy` |
| `tests/` | Unit tests plus an **integration test that runs the real mesh locally** (mTLS, canary, failover, timeout, breaker, tracing) |

## Quick start (on Ubuntu 24.04 with sudo)

```bash
chmod +x scripts/*.sh
./scripts/setup.sh        # installs Docker (once)
./scripts/preflight.sh    # pulls images, starts the mesh, runs smoke checks and tests
./scripts/up.sh           # (re)creates certificates and starts the mesh
./scripts/smoke.sh        # 12 checks, ends with "All 12 checks passed."
./scripts/demo.sh all     # seven scenarios
./scripts/destroy.sh      # remove everything (after the demo)
```

Open `http://<EC2-PUBLIC-IP>:8080/` (the app) and `:16686/` (Jaeger). See **STEPS.md** for the full walkthrough and **DEMO.md** for the one-page talk track.

## Notes

- Requirements: 2 vCPU, 4 GiB RAM or more, 6 GiB free disk. The whole mesh uses about 1 GiB of RAM.
- The sidecar configs were tested with the real Traefik binary on one machine (see `tests/test_mesh_integration.py`). The Docker packaging (containers sharing a network, image tags) can only be proven on the instance, which is what preflight is for.
- Certificates are valid for 30 days and made on the instance. They are for the demo only.
- Traefik does retries only for connection failures (not for HTTP 5xx replies), and its circuit breaker works on a whole upstream service, not on single instances. Real meshes such as Envoy and Istio can do more; the demo is built around what this proxy does well.
