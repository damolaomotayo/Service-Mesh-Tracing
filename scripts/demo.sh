#!/usr/bin/env bash
# Scripted demo. Usage: ./scripts/demo.sh [mtls|canary|header|failover|timeout|breaker|trace|all]
# Keep http://<EC2-PUBLIC-IP>:8080/ open in one browser tab and Jaeger (:16686) in another.
# Every scenario starts from the same known state (all traffic on v1, 3 retry attempts, nothing broken).
set -uo pipefail
. "$(dirname "$0")/lib.sh"
all_running || die "The mesh is not running. Run ./scripts/up.sh first."

FAILURES=0
assert() { # LABEL COMMAND...
  local label="$1"; shift
  if "$@"; then ok "$label"; else fail "$label"; FAILURES=$((FAILURES + 1)); fi
}
finish() {
  mesh_reset >/dev/null 2>&1
  if [ "$FAILURES" -eq 0 ]; then printf '\n\033[1;32mPASS\033[0m  %s\n\n' "$1"; else printf '\n\033[1;31mFAIL\033[0m  %s\n\n' "$1"; exit 1; fi
}
say() { printf '    %s\n' "$*"; }
fresh() { mesh_reset || { fail "could not reset the mesh. Run: ./scripts/smoke.sh"; exit 1; }; }
field() { # NAME "k=v k=v": the number for NAME (0 if absent)
  local part
  for part in $2; do [ "${part%%=*}" = "$1" ] && { echo "${part#*=}"; return; }; done
  echo 0
}
between() { [ "$1" -ge "$2" ] && [ "$1" -le "$3" ]; }
CERTS="$ROOT/certs"; MTLS_PORT=15101
contains() { [[ "$1" == *"$2"* ]]; }                      # contains TEXT NEEDLE
now_ms()   { echo $(( $(date +%s%N) / 1000000 )); }
body_now() { curl -s -m 10 "$GATEWAY_URL/api/products"; }
pricing_ok()   { contains "$(body_now)" '"pricing":"ok"'; }
v2_appears()   { contains "$($MESHCTL sample 30)" "v2="; }
only_v2()      { [ "$($MESHCTL sample 10)" = "failed=0 v2=10" ]; }
errors_appear() { contains "$($MESHCTL sample 20)" "failed=" && [ "$(field failed "$($MESHCTL sample 20)")" -ge 1 ]; }
pricing_hits() { $DOCKER exec mesh-pricing python -c "import json; print(json.load(open('/tmp/stats.json'))['requests'])" 2>/dev/null || echo 0; }
mtls_call() { # [CERT_NAME]: HTTP status of a direct call to catalog-v1's mesh port (000 = refused)
  local args=(--cacert "$CERTS/ca.pem" --resolve "catalog-v1:$MTLS_PORT:127.0.0.1")
  [ -n "${1:-}" ] && args+=(--cert "$CERTS/$1.pem" --key "$CERTS/$1.key")
  curl -s -o /dev/null -m 5 -w '%{http_code}' "${args[@]}" "https://catalog-v1:$MTLS_PORT/products" || true
}

scenario_mtls() {
  log "SCENARIO mtls: pods only talk to pods that hold a certificate from the mesh's own authority"
  fresh
  say "an intruder with no certificate calls the catalog's sidecar directly:"
  assert "refused (TLS handshake fails)"                   test "$(mtls_call)" = 000
  say "the same call with the frontend's mesh certificate:"
  assert "accepted (HTTP 200)"                             test "$(mtls_call frontend)" = 200
  assert "plain HTTP to the same port is not served"       test "$(http_code "http://127.0.0.1:$MTLS_PORT/products")" != 200
  say "(the apps themselves speak plain HTTP and know nothing about certificates)"
  finish mtls
}

scenario_canary() {
  log "SCENARIO canary: roll out catalog v2 gradually, live, without a deploy"
  fresh
  local r
  r="$($MESHCTL sample 40)"; say "start, 100% v1:        $r"; assert "all 40 requests answered by v1"        test "$r" = "failed=0 v1=40"
  $MESHCTL weights 90 10 >/dev/null; settle v2_appears
  r="$($MESHCTL sample 100)"; say "10% to v2:            $r"; assert "about 10 of 100 went to v2 (5 to 15)"   between "$(field v2 "$r")" 5 15
  $MESHCTL weights 50 50 >/dev/null; sleep 4
  r="$($MESHCTL sample 100)"; say "50/50:                $r"; assert "about half went to v2 (40 to 60)"      between "$(field v2 "$r")" 40 60
  $MESHCTL weights 0 100 >/dev/null; settle only_v2
  r="$($MESHCTL sample 40)"; say "v2 looks good, 100%:   $r"; assert "everything is on v2"                    test "$r" = "failed=0 v2=40"
  finish canary
}

scenario_header() {
  log "SCENARIO header: testers reach v2 with a header while real users stay on v1 (dark launch)"
  fresh
  local r
  r="$($MESHCTL sample 30)";                       say "normal users:         $r"; assert "normal users stay on v1"        test "$r" = "failed=0 v1=30"
  r="$($MESHCTL sample 30 -H x-canary:true)";      say "x-canary: true:       $r"; assert "the header sends them to v2"    test "$r" = "failed=0 v2=30"
  r="$($MESHCTL sample 10 -H x-canary:false)";     say "x-canary: false:      $r"; assert "other header values stay on v1" test "$r" = "failed=0 v1=10"
  say "(the apps only copy the header along; the sidecars do the routing)"
  finish header
}

scenario_failover() {
  log "SCENARIO failover: the v2 pod's sidecar crashes during a 50/50 canary. Retries hide it from users"
  fresh
  local r
  $MESHCTL weights 50 50 >/dev/null; sleep 4
  r="$($MESHCTL sample 40)"; say "50/50 and healthy:   $r"; assert "both versions answer, no failures"       test "$(field failed "$r")" -eq 0
  say "stopping the v2 sidecar (docker stop mesh-catalog-v2-sidecar)"
  $DOCKER stop mesh-catalog-v2-sidecar >/dev/null
  r="$($MESHCTL sample 100)"; say "retries = 3:          $r"
  assert "users saw almost no errors (97 or more of 100 succeeded)"      test "$(field v1 "$r")" -ge 97
  $MESHCTL retries 1 >/dev/null; settle errors_appear
  r="$($MESHCTL sample 100)"; say "retries = 1 (off):    $r"
  assert "without retries many requests fail (20 or more)"               test "$(field failed "$r")" -ge 20
  $DOCKER start mesh-catalog-v2-sidecar >/dev/null
  finish failover
}

scenario_timeout() {
  log "SCENARIO timeout: pricing becomes slow (3 s). The mesh gives up after 1 s and the page degrades instead of hanging"
  fresh
  local t0 ms body
  "$ROOT/scripts/chaos.sh" pricing delay_ms=3000 >/dev/null
  t0="$(now_ms)"; body="$(body_now)"; ms=$(( $(now_ms) - t0 ))
  say "answered in $ms ms: $body"
  assert "the page still answers"                          contains "$body" '"service":"frontend"'
  assert "the price is marked unavailable"                 contains "$body" unavailable
  assert "it took under 2.5 s (not 3 s or more)"           test "$ms" -lt 2500
  "$ROOT/scripts/chaos.sh" pricing clear >/dev/null
  finish timeout
}

scenario_breaker() {
  log "SCENARIO breaker: pricing keeps failing. The circuit opens, so we stop sending it traffic until it recovers"
  fresh
  local before after i degraded=0
  before="$(pricing_hits)"
  "$ROOT/scripts/chaos.sh" pricing error_rate=1 status=500 >/dev/null
  say "40 requests while pricing returns HTTP 500 to everyone:"
  for i in $(seq 40); do contains "$(body_now)" unavailable && degraded=$((degraded + 1)); done
  after="$(pricing_hits)"
  say "pages with the price marked unavailable: $degraded of 40;  calls that actually reached pricing: $((after - before))"
  assert "users still got pages (40 of 40, no hard failures)"            test "$degraded" -eq 40
  assert "the breaker shielded pricing (under 20 of 40 calls got through)" test "$((after - before))" -lt 20
  "$ROOT/scripts/chaos.sh" pricing clear >/dev/null
  say "pricing is healthy again; the circuit closes by itself after a few seconds"
  assert "the price comes back"                                          wait_until 30 pricing_ok
  finish breaker
}

scenario_trace() {
  log "SCENARIO trace: follow ONE request through all three services in Jaeger"
  fresh
  curl -s -m 5 -H "x-request-id: demo-$(date +%s)" "$GATEWAY_URL/api/products" >/dev/null
  say "looking it up in Jaeger (spans are exported every few seconds)..."
  local out; out="$($MESHCTL trace)"; say "$out"
  assert "one trace contains frontend, catalog and pricing"              contains "$out" "pricing-sidecar"
  say "open http://$(public_ip):16686/  >  Service: frontend-sidecar  >  Find Traces  >  click the newest one"
  finish trace
}

case "${1:-all}" in
  mtls)      scenario_mtls ;;
  canary)    scenario_canary ;;
  header)    scenario_header ;;
  failover)  scenario_failover ;;
  timeout)   scenario_timeout ;;
  breaker)   scenario_breaker ;;
  trace)     scenario_trace ;;
  all)       scenario_mtls; scenario_canary; scenario_header; scenario_failover; scenario_timeout; scenario_breaker; scenario_trace
             echo "All scenarios passed." ;;
  *)         die "usage: $0 [mtls|canary|header|failover|timeout|breaker|trace|all]" ;;
esac
