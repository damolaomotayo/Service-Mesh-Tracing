#!/usr/bin/env bash
# Creates the certificates and sidecar configs, builds the app image and starts the whole mesh.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

log "Creating the mesh certificates (mTLS) and the sidecar configs"
"$ROOT/scripts/certs.sh" || die "certificate creation failed (is openssl installed?)"
python3 "$ROOT/mesh/render.py" || die "could not render the sidecar configs (is python3-yaml installed?)"
$MESHCTL weights 100 0 >/dev/null && $MESHCTL retries 3 >/dev/null || die "could not reset the traffic rules"

log "Building the app image and starting 9 containers (4 apps, 4 sidecars, Jaeger)"
$DC up -d --build || die "docker compose up failed. Run: $DC logs --tail 40"

wait_until 90 mesh_ready || die "The mesh did not become ready. Run: $DOCKER logs --tail 30 mesh-frontend-sidecar"
ok "requests flow frontend -> catalog -> pricing through the sidecars (both catalog versions answer)"
wait_until 60 jaeger_ok || die "Jaeger did not start. Run: $DOCKER logs mesh-jaeger"
ok "Jaeger is up"

IP="$(public_ip)"
cat <<EOF

Mesh is up. Open in your browser (from the laptop allowed by the security group):
  App    : http://${IP}:8080/        <- frontend -> catalog -> pricing, shows which catalog version answered
  Traces : http://${IP}:16686/       <- Jaeger: pick service "frontend-sidecar" > Find Traces

Change traffic live:  ./scripts/mesh.sh canary 90 10      then refresh the app and watch v2 appear
Next: ./scripts/smoke.sh   then   ./scripts/demo.sh all
EOF
