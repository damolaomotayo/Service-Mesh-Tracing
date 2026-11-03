"""End-to-end test of the mesh on ONE machine with the REAL Traefik binary: 4 pods (app + sidecar) on loopback
addresses 127.0.10.1-4, real mTLS certificates, a fake OTLP collector. Proves mTLS, canary split, header routing,
failover/retries, timeout, circuit breaker and trace propagation. Skipped if traefik or openssl is missing.
"""
import gzip
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "mesh"))
import render  # noqa: E402

IPS = {"frontend": "127.0.10.1", "catalog-v1": "127.0.10.2", "catalog-v2": "127.0.10.3", "pricing": "127.0.10.4"}


def find_traefik():
    env = os.environ.get("TRAEFIK_BIN")
    if env and os.path.exists(env):
        return env
    return shutil.which("traefik")


try:
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
except ImportError:                                   # tracing assertions are skipped without it
    trace_service_pb2 = None


class Collector:
    """A fake OTLP/HTTP trace receiver that keeps every span it is sent."""
    def __init__(self):
        self.spans = []                               # (service_name, trace_id_hex, span_id_hex, parent_hex)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def read_body(self):                       # the OTLP exporter sends chunked bodies without a Content-Length
                if "chunked" in self.headers.get("Transfer-Encoding", ""):
                    data = b""
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            return data
                        data += self.rfile.read(size)
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def do_POST(self):
                body = self.read_body()
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                if trace_service_pb2 and self.path.endswith("/v1/traces"):
                    req = trace_service_pb2.ExportTraceServiceRequest()
                    req.ParseFromString(body)
                    for rs in req.resource_spans:
                        svc = next((a.value.string_value for a in rs.resource.attributes if a.key == "service.name"), "?")
                        for ss in rs.scope_spans:
                            for sp in ss.spans:
                                outer.spans.append((svc, sp.trace_id.hex(), sp.span_id.hex(), sp.parent_span_id.hex()))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Mesh:
    def __init__(self, traefik):
        self.traefik = traefik
        self.tmp = tempfile.mkdtemp(prefix="mesh-it-")
        self.procs = {}
        self.logs = []
        self.collector = Collector()
        for d in ("mesh", "app", "scripts"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.tmp, d), ignore=shutil.ignore_patterns("generated", "__pycache__"))
        subprocess.run(["bash", os.path.join(self.tmp, "scripts", "certs.sh"), os.path.join(self.tmp, "certs")], check=True, capture_output=True)
        self.out = os.path.join(self.tmp, "gen")
        self.net = {"ip": IPS, "certs": os.path.join(self.tmp, "certs"), "dynamic_dir": os.path.join(self.out, "{pod}", "dynamic"),
                    "tracing": f"http://127.0.0.1:{self.collector.port}/v1/traces"}
        render.render(self.tmp, self.net, self.out)

    # ---- processes ----
    def start_pod(self, pod):
        meta = render.load(self.tmp)[0]["pods"][pod]
        ip = IPS[pod]
        env = dict(os.environ, SERVICE=meta["service"], VERSION=meta.get("version", ""), POD=pod, HOST=ip, PORT="8000",
                   UPSTREAM_URL=f"http://{ip}:15001", CHAOS_FILE=os.path.join(self.tmp, f"{pod}.chaos"), STATS_FILE=os.path.join(self.tmp, f"{pod}.stats"))
        app = subprocess.Popen([sys.executable, os.path.join(self.tmp, "app", "service.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log = open(os.path.join(self.tmp, f"{pod}.traefik.log"), "w")
        self.logs.append(log)
        side = subprocess.Popen([self.traefik, f"--configFile={os.path.join(self.out, pod, 'traefik.yml')}"], stdout=log, stderr=log)
        self.procs[pod] = (app, side)

    def stop_pod(self, pod):
        for p in self.procs.pop(pod, ()):
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def start(self):
        for pod in IPS:
            self.start_pod(pod)
        self.wait_for(lambda: all(self.get(headers=h)[1]["catalog"]["pricing"] == "ok" for h in ({}, {"x-canary": "true"})), 40,
                      "the mesh to answer (both versions, pricing included)")

    def stop(self):
        for pod in list(self.procs):
            self.stop_pod(pod)
        self.collector.close()
        for log in self.logs:
            log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def get(self, path="/api/products", headers=None, timeout=10):
        req = urllib.request.Request(f"http://{IPS['frontend']}:8080{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return 0, None

    def versions(self, n=60, headers=None):
        seen = {}
        for _ in range(n):
            code, body = self.get(headers=headers)
            key = body["catalog"]["version"] if code == 200 and body else f"http{code}"
            seen[key] = seen.get(key, 0) + 1
        return seen

    def wait_for(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise AssertionError(f"timed out waiting for {what}")

    def traffic(self, **changes):
        path = os.path.join(self.tmp, "mesh", "traffic.yaml")
        with open(path) as fh:
            t = yaml.safe_load(fh)
        for k, v in changes.items():
            if k == "weights":
                t["catalog"]["weights"] = v
            else:
                t[k] = v
        with open(path, "w") as fh:
            yaml.safe_dump(t, fh)
        render.render(self.tmp, self.net, self.out)

    def chaos(self, pod, **cfg):
        with open(os.path.join(self.tmp, f"{pod}.chaos"), "w") as fh:
            json.dump(cfg, fh)

    def stat(self, pod):
        try:
            with open(os.path.join(self.tmp, f"{pod}.stats")) as fh:
                return json.load(fh)["requests"]
        except (OSError, ValueError, KeyError):
            return 0


@unittest.skipUnless(find_traefik() and shutil.which("openssl"), "traefik and openssl are needed for the mesh integration tests")
class MeshIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.mesh = Mesh(find_traefik())
        self.mesh.start()

    def tearDown(self):
        self.mesh.stop()

    def test_request_flows_frontend_catalog_pricing(self):
        code, body = self.mesh.get()
        self.assertEqual(code, 200)
        self.assertEqual(body["service"], "frontend")
        self.assertEqual(body["catalog"]["version"], "v1")
        self.assertEqual(body["catalog"]["pricing"], "ok")
        self.assertEqual(body["catalog"]["price"], 42.0)

    def test_html_page_for_the_browser(self):
        req = urllib.request.Request(f"http://{IPS['frontend']}:8080/")
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode()
        self.assertIn("catalog <b>v1</b>", page)

    def test_mtls_only_pods_with_a_mesh_certificate_get_in(self):
        host, port = IPS["catalog-v1"], 15006
        certs = os.path.join(self.mesh.tmp, "certs")

        def fetch(ctx):
            with socket.create_connection((host, port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="catalog-v1") as tls:
                    tls.sendall(b"GET /products HTTP/1.1\r\nHost: catalog-v1\r\nConnection: close\r\n\r\n")
                    return tls.recv(200)

        no_cert = ssl.create_default_context(cafile=os.path.join(certs, "ca.pem"))
        with self.assertRaises((ssl.SSLError, ConnectionError, OSError)):          # TLS 1.3: the rejection shows on first read
            fetch(no_cert)
        with_cert = ssl.create_default_context(cafile=os.path.join(certs, "ca.pem"))
        with_cert.load_cert_chain(os.path.join(certs, "frontend.pem"), os.path.join(certs, "frontend.key"))
        self.assertIn(b"200 OK", fetch(with_cert))
        # a certificate from some OTHER authority is refused too
        other = tempfile.mkdtemp(prefix="evilca-")
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
                            "-subj", "/CN=frontend", "-keyout", f"{other}/k.pem", "-out", f"{other}/c.pem"], check=True, capture_output=True)
            evil = ssl.create_default_context(cafile=os.path.join(certs, "ca.pem"))
            evil.load_cert_chain(f"{other}/c.pem", f"{other}/k.pem")
            with self.assertRaises((ssl.SSLError, ConnectionError, OSError)):
                fetch(evil)
        finally:
            shutil.rmtree(other, ignore_errors=True)
        # plain HTTP to the mTLS port gets nothing useful
        with socket.create_connection((host, port), timeout=5) as raw:
            raw.sendall(b"GET /products HTTP/1.1\r\nHost: x\r\n\r\n")
            raw.settimeout(5)
            try:
                reply = raw.recv(100)
            except OSError:
                reply = b""
        self.assertNotIn(b"200 OK", reply)

    @unittest.skipUnless(shutil.which("curl"), "curl not installed")
    def test_the_curl_commands_used_by_smoke_and_demo_scripts_behave(self):
        certs = os.path.join(self.mesh.tmp, "certs")
        base = ["curl", "-s", "-o", "/dev/null", "-m", "5", "-w", "%{http_code}", "--cacert", f"{certs}/ca.pem",
                "--resolve", f"catalog-v1:15006:{IPS['catalog-v1']}"]
        url = "https://catalog-v1:15006/products"
        noproxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        run = lambda cmd: subprocess.run(cmd, capture_output=True, text=True, env=noproxy)  # noqa: E731
        refused = run(base + [url])
        self.assertNotEqual(refused.returncode, 0)                               # no certificate: the handshake fails
        self.assertEqual(refused.stdout.strip(), "000")
        good = run(base + ["--cert", f"{certs}/frontend.pem", "--key", f"{certs}/frontend.key", url])
        self.assertEqual(good.stdout.strip(), "200")
        plain = run(["curl", "-s", "-o", "/dev/null", "-m", "5", "-w", "%{http_code}", f"http://{IPS['catalog-v1']}:15006/products"])
        self.assertNotEqual(plain.stdout.strip(), "200")

    def test_apps_are_not_reachable_without_their_sidecar_only_loopback_addresses_matter(self):
        """The catalog app listens on its own address, the sidecar port is the only mTLS one: plain http to 15006 fails."""
        with self.assertRaises(Exception):
            urllib.request.urlopen(f"http://{IPS['catalog-v1']}:15006/products", timeout=5).read()

    def test_canary_weights_move_live_traffic(self):
        m = self.mesh
        self.assertEqual(m.versions(30), {"v1": 30})
        m.traffic(weights={"v1": 50, "v2": 50})
        m.wait_for(lambda: len(m.versions(10)) == 2, 20, "50/50 split")
        seen = m.versions(100)
        self.assertTrue(40 <= seen.get("v2", 0) <= 60, seen)
        m.traffic(weights={"v1": 90, "v2": 10})
        m.wait_for(lambda: m.versions(40).get("v1", 0) >= 30, 20, "90/10 split")
        seen = m.versions(100)
        self.assertTrue(4 <= seen.get("v2", 0) <= 18, seen)
        m.traffic(weights={"v1": 0, "v2": 100})
        m.wait_for(lambda: m.versions(10) == {"v2": 10}, 20, "100% v2")

    def test_header_route_sends_testers_to_v2_while_everyone_else_stays_on_v1(self):
        m = self.mesh
        self.assertEqual(m.versions(20, headers={"x-canary": "true"}), {"v2": 20})
        self.assertEqual(m.versions(20), {"v1": 20})
        self.assertEqual(m.versions(10, headers={"x-canary": "false"}), {"v1": 10})

    def test_retries_hide_a_dead_canary_but_without_retries_users_see_errors(self):
        m = self.mesh
        m.traffic(weights={"v1": 50, "v2": 50})
        m.wait_for(lambda: len(m.versions(10)) == 2, 20, "50/50 split")
        m.stop_pod("catalog-v2")
        seen = m.versions(100)
        self.assertGreaterEqual(seen.get("v1", 0), 97, f"with retries: {seen}")
        m.traffic(retry_attempts=1)
        m.wait_for(lambda: m.versions(20).get("v1", 0) < 20, 20, "errors without retries")
        seen = m.versions(100)
        self.assertLessEqual(seen.get("v1", 0), 80, f"without retries: {seen}")

    def test_slow_dependency_times_out_in_one_second_and_the_page_degrades(self):
        m = self.mesh
        m.chaos("pricing", delay_ms=3000)
        t0 = time.time()
        code, body = m.get()
        took = time.time() - t0
        self.assertEqual(code, 200)
        self.assertEqual(body["catalog"]["pricing"], "unavailable")
        self.assertLess(took, 2.5, took)
        self.assertGreaterEqual(took, 0.9, took)

    def test_circuit_breaker_stops_hammering_a_failing_dependency_and_recovers(self):
        m = self.mesh
        m.chaos("pricing", error_rate=1, status=500)
        before = m.stat("pricing")
        for _ in range(40):
            code, body = m.get()
            self.assertEqual(code, 200)
            self.assertEqual(body["catalog"]["pricing"], "unavailable")
        reached = m.stat("pricing") - before
        self.assertLess(reached, 20, f"{reached} of 40 requests still reached pricing")
        m.chaos("pricing")                                                         # healthy again
        m.wait_for(lambda: m.get()[1]["catalog"]["pricing"] == "ok", 20, "the breaker to close again")

    @unittest.skipUnless(trace_service_pb2, "opentelemetry-proto not installed")
    def test_one_trace_spans_all_three_services(self):
        m = self.mesh
        code, _ = m.get(headers={"x-request-id": "demo-trace"})
        self.assertEqual(code, 200)
        m.wait_for(lambda: len({s[0] for s in m.collector.spans}) >= 3, 15, "spans from three sidecars")
        time.sleep(1.5)
        by_trace = {}
        for svc, tid, sid, parent in m.collector.spans:
            by_trace.setdefault(tid, []).append((svc, sid, parent))
        best = max(by_trace.values(), key=lambda spans: len({s[0] for s in spans}))
        services = {s[0] for s in best}
        self.assertEqual(services, {"frontend-sidecar", "catalog-v1-sidecar", "pricing-sidecar"}, services)
        ids = {s[1] for s in best}
        orphans = [s for s in best if s[2] and s[2] not in ids and s[0] != "frontend-sidecar"]
        self.assertEqual(orphans, [], "every non-root span must have its parent inside the same trace")


if __name__ == "__main__":
    unittest.main()
