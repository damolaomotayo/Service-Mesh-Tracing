#!/usr/bin/env bash
# Run this BEFORE the demo. It pulls the images, creates certificates, starts the whole mesh once, runs the
# smoke checks and the tests, so any failure shows up now and not in front of an audience.
# The mesh is left running; stop it with ./scripts/destroy.sh only AFTER the demo.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

FAILS=0
check() { # LABEL COMMAND...
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$label"; else fail "$label"; FAILS=$((FAILS + 1)); fi
}

pull_with_retry() { # IMAGE
  local i
  for i in 1 2 3; do
    $DOCKER pull "$1" >/dev/null 2>&1 && return 0
    sleep 3
  done
  return 1
}

# Docker Hub's /v2/ endpoint answers HTTP 401 (login required) when it is reachable.
# So 200 and 401 both mean "reachable". Do not add curl -f here.
hub_reachable() {
  local code
  code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' https://registry-1.docker.io/v2/ || true)"
  [ "$code" = "200" ] || [ "$code" = "401" ]
}
disk_ok()   { [ "$(df -BG --output=avail / | tail -n1 | tr -dc '0-9')" -ge 6 ]; }
mem_ok()    { [ "$(free -m | awk '/^Mem:/{print $2}')" -ge 3500 ]; }
port_free() { ! python3 -c 'import socket, sys; s = socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$1"; }
have_yaml() { python3 -c 'import yaml'; }
mesh_starts() { "$ROOT/scripts/up.sh"; }
smoke_passes() { "$ROOT/scripts/smoke.sh"; }
all_tests() { PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests; }

log "System checks"
check "Docker daemon reachable"                  $DOCKER info
check "Docker Compose v2 installed"              $DC version
check "openssl installed"                        openssl version
check "python3-yaml installed"                   have_yaml
check "At least 6 GB free disk"                  disk_ok
check "At least 3.5 GB RAM"                      mem_ok
if all_running; then
  ok "Mesh already running (port checks skipped)"
else
  for p in 8080 16686 15101; do check "Port $p is free" port_free "$p"; done
fi
check "Docker Hub reachable (HTTP 200 or 401)"   hub_reachable

log "Pulling images (up to 3 tries each)"
for img in "$TRAEFIK_IMAGE" "$JAEGER_IMAGE" python:3.12-slim; do
  check "Pull $img" pull_with_retry "$img"
done

log "Starting the mesh (certificates, sidecar configs, 9 containers)"
check "The mesh starts and requests flow"        mesh_starts
check "Smoke checks pass (12 checks)"            smoke_passes

log "Tests"
check "Unit tests and repo tests"                all_tests

echo
if [ "$FAILS" -eq 0 ]; then
  echo "Preflight passed. The mesh is running. Next: ./scripts/demo.sh all"
  exit 0
fi
echo "Preflight found $FAILS problem(s). See the troubleshooting table in STEPS.md." >&2
echo "Pull failed? Run 'sudo docker login' with a free Docker Hub account. Wrong tag? Edit the image in .env." >&2
exit 1
