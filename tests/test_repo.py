"""Sanity tests for the repo itself (layout, safety, config, docs). Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

REQUIRED_FILES = [
    "README.md", "STEPS.md", "DEMO.md", "DIAGRAM_PROMPT.md", ".gitignore", ".gitattributes", ".env.example",
    "docker-compose.yml", "app/service.py", "app/Dockerfile", "mesh/mesh.yaml", "mesh/traffic.yaml", "mesh/render.py",
    "certs/.gitkeep", "scripts/lib.sh", "scripts/setup.sh", "scripts/certs.sh", "scripts/preflight.sh", "scripts/up.sh",
    "scripts/smoke.sh", "scripts/demo.sh", "scripts/destroy.sh", "scripts/mesh.sh", "scripts/chaos.sh", "scripts/meshctl.py",
    "tests/test_render.py", "tests/test_meshctl.py", "tests/test_scripts.py", "tests/test_mesh_integration.py",
]
SCRIPTS = [f for f in REQUIRED_FILES if f.endswith(".sh")]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class LayoutTests(unittest.TestCase):
    def test_required_files_exist(self):
        for rel in REQUIRED_FILES:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), f"missing {rel}")

    def test_scripts_have_shebang(self):
        for rel in SCRIPTS:
            self.assertTrue(read(rel).startswith("#!/usr/bin/env bash"), rel)

    def test_no_crlf_line_endings(self):
        for rel in SCRIPTS + ["docker-compose.yml", "mesh/render.py", "scripts/meshctl.py"]:
            with open(os.path.join(ROOT, rel), "rb") as fh:
                self.assertNotIn(b"\r\n", fh.read(), rel)

    def test_generated_and_secret_files_are_not_shipped_and_are_ignored(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".env")))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "mesh", "generated")))
        self.assertEqual([f for f in os.listdir(os.path.join(ROOT, "certs")) if f != ".gitkeep"], [])
        ignore = read(".gitignore")
        for needle in (".env", "certs/*", "mesh/generated/", "*.pem"):
            self.assertIn(needle, ignore)

    def test_no_private_key_or_certificate_is_committed(self):
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="ignore") as fh:
                    self.assertNotIn("-----BEGIN " + "PRIVATE KEY", fh.read(), name)


class ConfigTests(unittest.TestCase):
    def test_only_the_gateway_the_ui_and_a_loopback_mtls_port_are_published(self):
        ports = [p for p in re.findall(r'-\s*"([^"]+)"', read("docker-compose.yml")) if re.match(r"^(\d+\.\d+\.\d+\.\d+:)?\d+:\d+$", p)]
        self.assertEqual(sorted(ports), ["127.0.0.1:15101:15006", "16686:16686", "8080:8080"])

    def test_images_are_official_and_pinned(self):
        env = dict(re.findall(r"^(\w+)=(.*)$", read(".env.example"), re.M))
        self.assertRegex(env["TRAEFIK_IMAGE"], r"^traefik:v3\.\d+\.\d+$")
        self.assertRegex(env["JAEGER_IMAGE"], r"^jaegertracing/all-in-one:1\.\d+\.\d+$")
        compose = read("docker-compose.yml")
        self.assertIn(env["TRAEFIK_IMAGE"], compose)
        self.assertIn(env["JAEGER_IMAGE"], compose)
        self.assertIn("FROM python:3.12-slim", read("app/Dockerfile"))

    def test_app_image_runs_as_a_non_root_user(self):
        self.assertIn("USER appuser", read("app/Dockerfile"))

    def test_apps_listen_on_loopback_only_by_default(self):
        self.assertIn('os.environ.get("HOST", "127.0.0.1")', read("app/service.py"))
        self.assertNotIn("HOST:", read("docker-compose.yml"))

    def test_apps_forward_the_headers_the_mesh_needs(self):
        text = read("app/service.py")
        for header in ("traceparent", "tracestate", "x-request-id", "x-canary"):
            self.assertIn(f'"{header}"', text)

    def test_app_uses_only_the_standard_library(self):
        imports = set(re.findall(r"^(?:import|from)\s+(\w+)", read("app/service.py"), re.M))
        self.assertTrue(imports <= {"json", "os", "random", "time", "urllib", "http"}, imports)

    def test_default_traffic_is_all_v1_with_three_attempts(self):
        text = read("mesh/traffic.yaml")
        self.assertIn("v1: 100", text)
        self.assertIn("v2: 0", text)
        self.assertIn("retry_attempts: 3", text)


class ScriptRuleTests(unittest.TestCase):
    def test_no_curl_f_against_docker_hub(self):
        for rel in SCRIPTS:
            for line in read(rel).splitlines():
                if "registry-1.docker.io" in line and "curl" in line:
                    self.assertNotRegex(line, r"curl\s+-\w*f", f"{rel}: {line}")

    def test_preflight_accepts_http_200_and_401_for_docker_hub(self):
        text = read("scripts/preflight.sh")
        self.assertIn('"$code" = "200"', text)
        self.assertIn('"$code" = "401"', text)

    def test_no_pipe_into_grep_q(self):
        for rel in SCRIPTS:
            for n, line in enumerate(read(rel).splitlines(), 1):
                if not line.strip().startswith("#"):
                    self.assertNotRegex(line, r"\|\s*grep\s+-\w*q", f"{rel}:{n}: {line}")

    def test_demo_has_all_scenarios_and_smoke_has_12_checks(self):
        demo = read("scripts/demo.sh")
        for name in ("mtls", "canary", "header", "failover", "timeout", "breaker", "trace"):
            self.assertIn(f"scenario_{name}()", demo)
        smoke = read("scripts/smoke.sh")
        self.assertEqual(len(re.findall(r"^check ", smoke, re.M)), 12)
        self.assertIn('All $TOTAL checks passed.', smoke)

    def test_demo_does_not_exit_on_the_first_failed_check(self):
        self.assertNotRegex(read("scripts/demo.sh"), r"^set -\w*e")

    def test_demo_does_not_need_bc_or_jq(self):
        for rel in SCRIPTS:
            self.assertNotRegex(read(rel), r"\b(bc|jq)\b -", rel)
            self.assertNotIn("| bc", read(rel))

    def test_lib_lists_every_container_in_the_compose_file(self):
        names = set(re.findall(r"container_name: (\S+)", read("docker-compose.yml")))
        self.assertEqual(set(re.search(r'CONTAINERS="([^"]+)"', read("scripts/lib.sh")).group(1).split()), names)

    def test_setup_installs_yaml_and_openssl(self):
        text = read("scripts/setup.sh")
        self.assertIn("python3-yaml", text)
        self.assertIn("openssl", text)


class DocsTests(unittest.TestCase):
    def test_steps_has_prerequisites_first_and_the_key_items(self):
        text = read("STEPS.md")
        self.assertLess(text.index("## Prerequisites"), text.index("## Step 0"))
        for needle in ("8080", "16686", "chmod +x scripts/*.sh", "chmod 400", "All 12 checks passed.", "sudo apt update",
                       "Terminate", "My IP", "m7i-flex.large", "preflight.sh"):
            self.assertIn(needle, text)

    def test_every_script_is_documented_in_steps(self):
        steps = read("STEPS.md")
        for name in ("setup.sh", "preflight.sh", "up.sh", "smoke.sh", "demo.sh", "destroy.sh", "mesh.sh", "chaos.sh"):
            self.assertIn(name, steps)

    def test_demo_md_is_very_short(self):
        text = read("DEMO.md")
        self.assertLess(len(text.split()), 450)
        self.assertLess(len(text.splitlines()), 40)

    def test_readme_is_honest_about_what_the_mesh_is(self):
        self.assertIn("Traefik", read("README.md"))
        self.assertIn("not Istio", read("README.md"))

    def test_diagram_prompt_mentions_the_main_parts(self):
        text = read("DIAGRAM_PROMPT.md")
        for needle in ("sidecar", "mTLS", "Jaeger", "8080", "16686", "canary", "circuit breaker"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
