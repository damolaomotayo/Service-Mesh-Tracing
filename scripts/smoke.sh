#!/usr/bin/env bash
# Health check of the running mesh (run ./scripts/up.sh first). Ends with "All 12 checks passed."
set -uo pipefail
. "$(dirname "$0")/lib.sh"

TOTAL=0; BAD=0
check() { # LABEL COMMAND...
  local label="$1"; shift
  TOTAL=$((TOTAL + 1))
  if "$@" >/dev/null 2>&1; then ok "$label"; else fail "$label"; BAD=$((BAD + 1)); fi
}
MTLS_PORT=15101        # catalog-v1's mTLS port, published on this machine's loopback only
CERTS="$ROOT/certs"
body_has() { local body; body="$(curl -s -m 5 "$GATEWAY_URL$1")"; [[ "$body" == *"$2"* ]]; }
full_chain()      { body_has /api/products '"service":"frontend"' && body_has /api/products '"service":"catalog"' && body_has /api/products '"pricing":"ok"'; }
html_page()       { body_has / "Mesh demo"; }
no_cert_refused() { ! curl -s -o /dev/null -m 5 --cacert "$CERTS/ca.pem" --resolve "catalog-v1:$MTLS_PORT:127.0.0.1" "https://catalog-v1:$MTLS_PORT/products"; }
with_cert_works() { [ "$(curl -s -o /dev/null -m 5 -w '%{http_code}' --cacert "$CERTS/ca.pem" --cert "$CERTS/frontend.pem" --key "$CERTS/frontend.key" \
                      --resolve "catalog-v1:$MTLS_PORT:127.0.0.1" "https://catalog-v1:$MTLS_PORT/products")" = "200" ]; }
plain_http_refused() { [ "$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$MTLS_PORT/products")" != "200" ]; }
all_v1()          { [ "$($MESHCTL sample 20)" = "failed=0 v1=20" ]; }
header_goes_v2()  { [ "$($MESHCTL sample 10 -H x-canary:true)" = "failed=0 v2=10" ]; }
trace_ok()        { $MESHCTL trace; }

check "Docker daemon reachable"                              $DOCKER info
check "all 9 containers are running"                         all_running
check "the gateway answers on port 8080"                     chain_ok
check "request chain frontend > catalog > pricing works"     full_chain
check "the browser page renders"                             html_page
check "mTLS: a client without a certificate is refused"      no_cert_refused
check "mTLS: a client with a mesh certificate gets in"       with_cert_works
check "mTLS: plain HTTP to the mesh port is refused"         plain_http_refused
check "traffic: 100/0 split sends everything to v1"          all_v1
check "traffic: the x-canary header goes to v2"              header_goes_v2
check "Jaeger answers on port 16686"                         jaeger_ok
check "one trace spans 3 services (frontend, catalog, pricing)" trace_ok

echo
if [ "$BAD" -eq 0 ]; then echo "All $TOTAL checks passed."; exit 0; fi
echo "$BAD of $TOTAL checks failed." >&2
exit 1
