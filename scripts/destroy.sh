#!/usr/bin/env bash
# Removes everything this project created: the 9 containers, the app image, the certificates and the sidecar configs.
# Usage: ./scripts/destroy.sh [--keep-env] [--images]     (--images also deletes the traefik, jaeger and python images)
set -uo pipefail
. "$(dirname "$0")/lib.sh"

KEEP_ENV=""; IMAGES=""
for a in "$@"; do case "$a" in --keep-env) KEEP_ENV=1 ;; --images) IMAGES=1 ;; *) die "usage: $0 [--keep-env] [--images]" ;; esac; done

log "Stopping and removing the containers"
$DC down --remove-orphans
leftover="$($DOCKER ps -aq --filter 'name=^mesh-')"
[ -z "$leftover" ] || $DOCKER rm -f $leftover >/dev/null

log "Removing the app image"
$DOCKER rmi -f mesh-app:local >/dev/null 2>&1 || true
if [ -n "$IMAGES" ]; then
  for img in "$TRAEFIK_IMAGE" "$JAEGER_IMAGE" python:3.12-slim; do $DOCKER rmi -f "$img" >/dev/null 2>&1 || true; done
fi

rm -rf "$ROOT/mesh/generated" "$ROOT"/certs/*.pem "$ROOT"/certs/*.key "$ROOT"/certs/*.srl
[ -n "$KEEP_ENV" ] || rm -f "$ROOT/.env"
$MESHCTL weights 100 0 >/dev/null 2>&1; $MESHCTL retries 3 >/dev/null 2>&1; rm -rf "$ROOT/mesh/generated"   # traffic rules back to the defaults

echo
echo "Cleanup done. (Images stay cached; add --images to delete them too.)"
echo "IMPORTANT: terminate the EC2 instance in the AWS console (EC2 > Instances >"
echo "Instance state > Terminate) so it stops costing money. Then delete the security group."
