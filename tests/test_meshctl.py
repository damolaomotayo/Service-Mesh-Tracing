"""Tests scripts/meshctl.py: editing the traffic rules, sampling the gateway, reading traces from Jaeger."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import meshctl  # noqa: E402


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meshctl-")
        for d in ("mesh",):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.tmp, d), ignore=shutil.ignore_patterns("generated", "__pycache__"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_weights_are_saved_and_rendered(self):
        meshctl.set_weights(70, 30, self.tmp)
        self.assertEqual(meshctl.load_traffic(self.tmp)["catalog"]["weights"], {"v1": 70, "v2": 30})
        with open(os.path.join(self.tmp, "mesh", "generated", "frontend", "dynamic", "mesh.yml")) as fh:
            text = fh.read()
        self.assertIn("weight: 70", text)
        self.assertIn("weight: 30", text)

    def test_header_of_the_file_and_other_rules_survive_an_edit(self):
        meshctl.set_weights(0, 100, self.tmp)
        with open(os.path.join(self.tmp, "mesh", "traffic.yaml")) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("# Traffic rules"))
        self.assertIn("header_route", text)
        self.assertEqual(meshctl.load_traffic(self.tmp)["retry_attempts"], 3)

    def test_bad_values_are_refused_and_nothing_is_written(self):
        with open(os.path.join(self.tmp, "mesh", "traffic.yaml")) as fh:
            before = fh.read()
        for bad in ((0, 0), (-5, 105)):
            with self.assertRaises(ValueError):
                meshctl.set_weights(*bad, root=self.tmp)
        with self.assertRaises(ValueError):
            meshctl.set_retries(0, self.tmp)
        with open(os.path.join(self.tmp, "mesh", "traffic.yaml")) as fh:
            self.assertEqual(before, fh.read())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mesh", "generated")))

    def test_retries(self):
        meshctl.set_retries(1, self.tmp)
        self.assertEqual(meshctl.load_traffic(self.tmp)["retry_attempts"], 1)


class FakeServer:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SamplingTests(unittest.TestCase):
    def test_sample_counts_versions_and_failures_and_sends_headers(self):
        seen = {"headers": [], "n": 0}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["n"] += 1
                seen["headers"].append(self.headers.get("x-canary"))
                if seen["n"] % 5 == 0:
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({"catalog": {"version": "v2" if seen["n"] % 2 else "v1"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = FakeServer(H)
        try:
            counts = meshctl.sample(10, {"x-canary": "true"}, srv.url)
        finally:
            srv.close()
        self.assertEqual(counts, {"failed": 2, "v1": 4, "v2": 4})
        self.assertEqual(set(seen["headers"]), {"true"})

    def test_sample_against_nothing_counts_failures(self):
        self.assertEqual(meshctl.sample(3, None, "http://127.0.0.1:1"), {"failed": 3})


class TraceTests(unittest.TestCase):
    DATA = {"data": [
        {"traceID": "aa", "spans": [{}] * 3, "processes": {"p1": {"serviceName": "frontend-sidecar"}}},
        {"traceID": "bb", "spans": [{}] * 10, "processes": {"p1": {"serviceName": "frontend-sidecar"}, "p2": {"serviceName": "catalog-v1-sidecar"},
                                                           "p3": {"serviceName": "pricing-sidecar"}}}]}

    def test_picks_the_trace_with_the_most_services(self):
        tid, names, spans = meshctl.trace_services(self.DATA)
        self.assertEqual((tid, spans), ("bb", 10))
        self.assertEqual(names, ["catalog-v1-sidecar", "frontend-sidecar", "pricing-sidecar"])

    def test_empty_reply(self):
        self.assertEqual(meshctl.trace_services({"data": None}), (None, [], 0))

    def test_find_trace_reads_the_jaeger_api(self):
        data = self.DATA

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = FakeServer(H)
        try:
            self.assertEqual(meshctl.find_trace(srv.url, wait=5)[1][-1], "pricing-sidecar")
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
