"""Tests mesh/render.py (the sidecar config generator) and scripts/certs.sh. No Docker and no proxy needed."""
import copy
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "mesh"))
import render  # noqa: E402


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.mesh, self.traffic = render.load(ROOT)

    def cfg(self, pod, traffic=None):
        return render.sidecar_config(pod, self.mesh, traffic or self.traffic)

    def test_entry_points_per_pod(self):
        eps = {pod: set(self.cfg(pod)[0]["entryPoints"]) for pod in self.mesh["pods"]}
        self.assertEqual(eps["frontend"], {"inbound", "outbound", "gateway"})        # only the frontend is public
        self.assertEqual(eps["catalog-v1"], {"inbound", "outbound"})
        self.assertEqual(eps["pricing"], {"inbound"})                                # a leaf service calls nobody

    def test_app_is_only_reachable_through_its_own_sidecar(self):
        for pod in self.mesh["pods"]:
            svc = self.cfg(pod)[1]["http"]["services"]["local-app"]["loadBalancer"]["servers"][0]["url"]
            self.assertEqual(svc, "http://127.0.0.1:8000")
            static = self.cfg(pod)[0]["entryPoints"]
            if "outbound" in static:
                self.assertTrue(static["outbound"]["address"].startswith("127.0.0.1:"), "the outbound port must be loopback only")

    def test_inbound_requires_a_client_certificate_signed_by_the_mesh_ca(self):
        for pod in self.mesh["pods"]:
            dyn = self.cfg(pod)[1]
            opts = dyn["tls"]["options"]["default"]
            self.assertEqual(opts["clientAuth"]["clientAuthType"], "RequireAndVerifyClientCert")
            self.assertEqual(opts["clientAuth"]["caFiles"], ["/certs/ca.pem"])
            self.assertEqual(dyn["tls"]["certificates"][0]["certFile"], f"/certs/{pod}.pem")
            self.assertEqual(dyn["http"]["routers"]["in"]["entryPoints"], ["inbound"])
            self.assertIn("tls", dyn["http"]["routers"]["in"])

    def test_outbound_calls_use_mtls_and_verify_the_server_name(self):
        dyn = self.cfg("frontend")[1]["http"]
        for name in ("catalog-v1", "catalog-v2"):
            t = dyn["serversTransports"][f"to-{name}"]
            self.assertEqual(t["serverName"], name)
            self.assertEqual(t["rootCAs"], ["/certs/ca.pem"])
            self.assertEqual(t["certificates"][0]["certFile"], "/certs/frontend.pem")        # presents ITS OWN certificate
            self.assertEqual(dyn["services"][name]["loadBalancer"]["servers"][0]["url"], f"https://{name}:15006")

    def test_weights_and_zero_weight_versions(self):
        t = copy.deepcopy(self.traffic)
        t["catalog"]["weights"] = {"v1": 90, "v2": 10}
        services = self.cfg("frontend", t)[1]["http"]["services"]["catalog"]["weighted"]["services"]
        self.assertEqual({s["name"]: s["weight"] for s in services}, {"catalog-v1": 90, "catalog-v2": 10})
        t["catalog"]["weights"] = {"v1": 100, "v2": 0}
        services = self.cfg("frontend", t)[1]["http"]["services"]["catalog"]["weighted"]["services"]
        self.assertEqual([s["name"] for s in services], ["catalog-v1"])                     # a 0% version is left out

    def test_header_route_has_priority_and_goes_to_the_named_version(self):
        r = self.cfg("frontend")[1]["http"]["routers"]
        hdr = r["to-catalog-by-header"]
        self.assertGreater(hdr["priority"], 50)
        self.assertEqual(hdr["service"], "catalog-v2")
        self.assertIn("Header(`x-canary`, `true`)", hdr["rule"])
        t = copy.deepcopy(self.traffic)
        del t["catalog"]["header_route"]
        self.assertNotIn("to-catalog-by-header", self.cfg("frontend", t)[1]["http"]["routers"])

    def test_breaker_wraps_retry_so_it_judges_what_users_get(self):
        r = self.cfg("frontend")[1]["http"]["routers"]["to-catalog"]
        self.assertEqual(r["middlewares"], ["breaker", "retry"])

    def test_retry_attempts_and_per_service_timeouts(self):
        t = copy.deepcopy(self.traffic)
        t["retry_attempts"] = 1
        self.assertEqual(self.cfg("frontend", t)[1]["http"]["middlewares"]["retry"]["retry"]["attempts"], 1)
        # a caller must wait longer than the service it calls, or one slow service times out the whole chain
        catalog_wait = self.cfg("frontend")[1]["http"]["serversTransports"]["to-catalog-v1"]["forwardingTimeouts"]["responseHeaderTimeout"]
        pricing_wait = self.cfg("catalog-v1")[1]["http"]["serversTransports"]["to-pricing"]["forwardingTimeouts"]["responseHeaderTimeout"]
        secs = lambda v: float(v.rstrip("s"))  # noqa: E731
        self.assertGreater(secs(catalog_wait), secs(pricing_wait))

    def test_each_sidecar_traces_under_its_own_name(self):
        names = {pod: self.cfg(pod)[0]["tracing"]["serviceName"] for pod in self.mesh["pods"]}
        self.assertEqual(names["catalog-v2"], "catalog-v2-sidecar")
        self.assertEqual(len(set(names.values())), len(names))
        self.assertTrue(self.cfg("pricing")[0]["tracing"]["otlp"]["http"]["endpoint"].startswith("http://jaeger:4318/"))

    def test_validation_catches_mistakes(self):
        bad = copy.deepcopy(self.traffic)
        bad["catalog"]["weights"] = {"v1": 0, "v2": 0}
        with self.assertRaises(ValueError):
            render.validate(self.mesh, bad)
        bad["catalog"]["weights"] = {"v9": 100}
        with self.assertRaises(ValueError):
            render.validate(self.mesh, bad)
        bad = copy.deepcopy(self.traffic)
        bad["catalog"]["header_route"]["version"] = "v7"
        with self.assertRaises(ValueError):
            render.validate(self.mesh, bad)
        bad = copy.deepcopy(self.traffic)
        bad["retry_attempts"] = 0
        with self.assertRaises(ValueError):
            render.validate(self.mesh, bad)
        mesh = copy.deepcopy(self.mesh)
        mesh["pods"]["pricing"]["upstream"] = "ghost"
        with self.assertRaises(ValueError):
            render.validate(mesh, self.traffic)

    def test_rendered_files_are_clean_yaml_without_leftovers(self):
        tmp = tempfile.mkdtemp(prefix="render-")
        try:
            render.render(ROOT, out=tmp)
            self.assertEqual(sorted(os.listdir(tmp)), sorted(self.mesh["pods"]))
            for pod in self.mesh["pods"]:
                self.assertEqual(sorted(os.listdir(os.path.join(tmp, pod))), ["dynamic", "traefik.yml"])
                self.assertEqual(os.listdir(os.path.join(tmp, pod, "dynamic")), ["mesh.yml"])      # no temp files left behind
                for f in ("traefik.yml", os.path.join("dynamic", "mesh.yml")):
                    with open(os.path.join(tmp, pod, f)) as fh:
                        text = fh.read()
                    self.assertTrue(text.startswith("# GENERATED"))
                    self.assertNotIn("&id0", text)                                              # no YAML anchors
                    self.assertIsInstance(yaml.safe_load(text), dict)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_every_pod_is_in_the_compose_file_with_a_sidecar_sharing_its_network(self):
        with open(os.path.join(ROOT, "docker-compose.yml")) as fh:
            compose = yaml.safe_load(fh)["services"]
        for pod in self.mesh["pods"]:
            self.assertIn(pod, compose)
            side = compose[f"{pod}-sidecar"]
            self.assertEqual(side["network_mode"], f"service:{pod}")
            self.assertIn(f"./mesh/generated/{pod}:/etc/traefik:ro", side["volumes"])
            self.assertIn("./certs:/certs:ro", side["volumes"])
            self.assertEqual(compose[pod]["container_name"], f"mesh-{pod}")
            self.assertEqual(side["container_name"], f"mesh-{pod}-sidecar")
            self.assertEqual(compose[pod]["environment"]["SERVICE"], self.mesh["pods"][pod]["service"])
            self.assertEqual(compose[pod]["environment"].get("VERSION"), self.mesh["pods"][pod].get("version"))


@unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
class CertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="certs-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_certs(self):
        return subprocess.run(["bash", os.path.join(ROOT, "scripts", "certs.sh"), self.tmp], capture_output=True, text=True)

    def test_one_certificate_per_pod_signed_by_the_mesh_ca_and_valid_for_both_directions(self):
        self.assertEqual(self.run_certs().returncode, 0)
        pods = render.load(ROOT)[0]["pods"]
        for pod in pods:
            ok = subprocess.run(["openssl", "verify", "-CAfile", f"{self.tmp}/ca.pem", f"{self.tmp}/{pod}.pem"], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            text = subprocess.run(["openssl", "x509", "-in", f"{self.tmp}/{pod}.pem", "-noout", "-text"], capture_output=True, text=True).stdout
            self.assertIn(f"DNS:{pod}", text)
            self.assertIn("TLS Web Server Authentication", text)
            self.assertIn("TLS Web Client Authentication", text)

    def test_rerun_keeps_the_existing_certificates(self):
        self.run_certs()
        with open(os.path.join(self.tmp, "frontend.pem")) as fh:
            before = fh.read()
        self.run_certs()
        with open(os.path.join(self.tmp, "frontend.pem")) as fh:
            self.assertEqual(before, fh.read())


if __name__ == "__main__":
    unittest.main()
