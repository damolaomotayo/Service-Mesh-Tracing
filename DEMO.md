# Demo cheat sheet (1-minute read)

**What it does:** In a **service mesh**, every service gets a small proxy next to it (a **sidecar**). The proxies handle the hard network work (encryption, routing, retries, timeouts, tracing) so the apps stay simple. Here a frontend, a catalog (v1 and v2) and a pricing service run as 4 pods on this one EC2 instance, each with a sidecar. The proxy used is Traefik, and traces go to Jaeger.

**How it works:**
1. Browser, then the **frontend**'s sidecar (port 8080), then the app. The app calls the next service through its own sidecar on `127.0.0.1`.
2. Sidecar to sidecar traffic uses **mTLS**: both sides show a certificate from the mesh's own authority, or the connection is refused.
3. `mesh/traffic.yaml` holds the rules (canary split, header routes, retries). Change it and the sidecars reload live.
4. Every hop sends a span to **Jaeger**, so one request shows up as one trace across all services.

**Example 1: canary release (`./scripts/demo.sh canary`)**
Open `http://<PUBLIC-IP>:8080/` and refresh: it says catalog **v1**. Run `./scripts/mesh.sh canary 90 10`: now about 1 refresh in 10 says **v2**. Go 50/50, then 0/100. No deploy, no restart.

**Example 2: the crash nobody notices (`./scripts/demo.sh failover`)**
Half the traffic goes to v2. Its sidecar is stopped. Users still get answers because the mesh **retries** on the healthy version. Turn retries off and about half the requests fail.

**Bonus:** `mtls` (an intruder is refused), `header` (testers send `x-canary: true` to reach v2), `timeout` (slow pricing is cut off after 1 s and the page still loads), `breaker` (a failing service is left alone until it recovers), `trace` (one request across 3 services in Jaeger on port 16686).

**If asked "is this Istio?":** No. It is the same idea (sidecar proxies plus a rules file) built from Traefik, so it fits one small instance. Istio and Linkerd add an automatic control plane and run on Kubernetes.
