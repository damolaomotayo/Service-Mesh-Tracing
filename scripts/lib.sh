#!/usr/bin/env bash
# Shared helpers. Sourced by the other scripts, not run directly.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# Use plain docker if the current user may talk to the daemon, otherwise sudo.
if docker info >/dev/null 2>&1; then DOCKER="docker"; else DOCKER="sudo docker"; fi
DC="$DOCKER compose"

GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8080}"
JAEGER_URL="${JAEGER_URL:-http://127.0.0.1:16686}"
export GATEWAY_URL JAEGER_URL
PODS="frontend catalog-v1 catalog-v2 pricing"
CONTAINERS="mesh-jaeger mesh-frontend mesh-frontend-sidecar mesh-catalog-v1 mesh-catalog-v1-sidecar mesh-catalog-v2 mesh-catalog-v2-sidecar mesh-pricing mesh-pricing-sidecar"
MESHCTL="python3 $ROOT/scripts/meshctl.py"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32mok\033[0m   %s\n' "$*"; }
fail() { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

ensure_env() {
  [ -f "$ROOT/.env" ] || cp "$ROOT/.env.example" "$ROOT/.env"
  set -a; . "$ROOT/.env"; set +a
}
ensure_env

public_ip() { # EC2 metadata (IMDSv2), then a public what-is-my-ip fallback
  local tok ip=""
  tok="$(curl -fsS -m 2 -X PUT http://169.254.169.254/latest/api/token \
         -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)"
  if [ -n "$tok" ]; then
    ip="$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $tok" \
          http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
  fi
  if [ -z "$ip" ]; then
    ip="$(curl -fsS -m 4 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
  fi
  echo "${ip:-<EC2-PUBLIC-IP>}"
}

http_code() { curl -s -o /dev/null -m 4 -w '%{http_code}' "$1" 2>/dev/null || true; }   # 000 = no connection

wait_until() { # SECONDS COMMAND...   (retries every 2 seconds)
  local secs="$1" i; shift
  for ((i = 0; i < secs; i += 2)); do
    "$@" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

# ---- the mesh ----
container_running() { [ "$($DOCKER ps -q --filter "name=^$1\$" | wc -l)" -gt 0 ]; }
all_running() { local c; for c in $CONTAINERS; do container_running "$c" || return 1; done; }
# the request path works only when frontend, catalog and pricing (and both catalog versions) are all serving
chain_ok()  { [ "$(http_code "$GATEWAY_URL/api/products")" = "200" ]; }
priced()    { local body; body="$(curl -s -m 4 "$@" "$GATEWAY_URL/api/products")"; [[ "$body" == *'"pricing":"ok"'* ]]; }
v2_ok()     { priced -H 'x-canary: true'; }
mesh_ready() { chain_ok && priced && v2_ok; }
jaeger_ok() { [ "$(http_code "$JAEGER_URL/")" = "200" ]; }
# the sidecars watch their config folder; if a change is not picked up, restarting a sidecar always works
settle() { # COMMAND...: wait up to 12 s for COMMAND to succeed, then restart all sidecars once and wait again
  wait_until 12 "$@" && return 0
  local p; for p in $PODS; do $DOCKER restart "mesh-$p-sidecar" >/dev/null 2>&1; done
  wait_until 30 "$@"
}
mesh_reset() { # known starting point: all traffic to v1, 3 attempts, no chaos, every sidecar running
  local p
  for p in $PODS; do container_running "mesh-$p-sidecar" || $DOCKER start "mesh-$p-sidecar" >/dev/null 2>&1; done
  "$ROOT/scripts/chaos.sh" all clear >/dev/null 2>&1
  $MESHCTL retries 3 >/dev/null && $MESHCTL weights 100 0 >/dev/null || return 1
  settle only_v1
}
only_v1() { [ "$($MESHCTL sample 12)" = "failed=0 v1=12" ]; }
