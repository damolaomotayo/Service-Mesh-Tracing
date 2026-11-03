"""Tests the shell scripts that can run without Docker (chaos.sh, mesh.sh, lib.sh helpers) using a stub `docker`."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

STUB = r'''#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$1" in
  info) exit 0 ;;
  ps)   for c in $STUB_RUNNING; do case "$*" in *"name=^$c\$"*) echo abc123 ;; esac; done ;;
esac
exit 0
'''


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mesh-scripts-")
        for d in ("scripts", "mesh"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.tmp, d), ignore=shutil.ignore_patterns("generated", "__pycache__"))
        shutil.copy(os.path.join(ROOT, ".env.example"), self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "docker")
        with open(stub, "w") as fh:
            fh.write(STUB)
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp, "docker.log")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *cmd, **env):
        full = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], STUB_LOG=self.log, **env)
        return subprocess.run(["bash", *cmd], cwd=self.tmp, env=full, capture_output=True, text=True, timeout=60)

    def logged(self):
        with open(self.log) as fh:
            return fh.read()

    def test_env_file_is_created_from_the_example(self):
        self.run_script("-c", f'. "{self.tmp}/scripts/lib.sh"')
        with open(os.path.join(self.tmp, ".env")) as fh:
            self.assertIn("TRAEFIK_IMAGE=traefik:v3", fh.read())

    def test_chaos_sets_numbers_in_the_right_container(self):
        res = self.run_script("scripts/chaos.sh", "pricing", "delay_ms=3000", "error_rate=0.5")
        self.assertEqual(res.returncode, 0, res.stderr)
        log = self.logged()
        self.assertIn("exec mesh-pricing python -c", log)
        self.assertTrue(log.rstrip().endswith("delay_ms=3000 error_rate=0.5"))

    def test_chaos_clear_all_touches_every_pod(self):
        self.run_script("scripts/chaos.sh", "all", "clear")
        targets = [x.split()[1] for x in self.logged().splitlines() if x.startswith("exec")]
        self.assertEqual(targets, ["mesh-frontend", "mesh-catalog-v1", "mesh-catalog-v2", "mesh-pricing"])

    def test_chaos_rejects_unknown_pods_and_missing_arguments(self):
        self.assertNotEqual(self.run_script("scripts/chaos.sh", "ghost", "clear").returncode, 0)
        self.assertNotEqual(self.run_script("scripts/chaos.sh", "pricing").returncode, 0)

    def test_mesh_canary_edits_traffic_and_rerenders(self):
        res = self.run_script("scripts/mesh.sh", "canary", "80", "20")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with open(os.path.join(self.tmp, "mesh", "traffic.yaml")) as fh:
            text = fh.read()
        self.assertIn("v1: 80", text)
        self.assertIn("v2: 20", text)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "mesh", "generated", "frontend", "dynamic", "mesh.yml")))
        self.assertNotEqual(self.run_script("scripts/mesh.sh", "canary", "0", "0").returncode, 0)
        self.assertNotEqual(self.run_script("scripts/mesh.sh", "canary", "50").returncode, 0)
        self.assertNotEqual(self.run_script("scripts/mesh.sh", "nonsense").returncode, 0)

    def test_mesh_status_prints_the_rules(self):
        res = self.run_script("scripts/mesh.sh", "status")
        self.assertIn("retry_attempts: 3", res.stdout)

    def test_all_running_needs_all_nine_containers(self):
        names = "mesh-jaeger mesh-frontend mesh-frontend-sidecar mesh-catalog-v1 mesh-catalog-v1-sidecar mesh-catalog-v2 mesh-catalog-v2-sidecar mesh-pricing mesh-pricing-sidecar"
        lib = f'. "{self.tmp}/scripts/lib.sh"; all_running; echo rc=$?'
        self.assertIn("rc=0", self.run_script("-c", lib, STUB_RUNNING=names).stdout)
        self.assertIn("rc=1", self.run_script("-c", lib, STUB_RUNNING=names.replace("mesh-pricing-sidecar", "")).stdout)

    def test_destroy_removes_generated_files_and_resets_traffic(self):
        self.run_script("scripts/mesh.sh", "canary", "10", "90")
        res = self.run_script("scripts/destroy.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with open(os.path.join(self.tmp, "mesh", "traffic.yaml")) as fh:
            self.assertIn("v1: 100", fh.read())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".env")))
        self.assertIn("rmi -f mesh-app:local", self.logged())
        self.assertIn("Terminate", res.stdout)


if __name__ == "__main__":
    unittest.main()
