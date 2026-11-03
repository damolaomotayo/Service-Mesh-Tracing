#!/usr/bin/env bash
# Change the mesh's traffic rules live (no restarts).
#   ./scripts/mesh.sh status                 show the current rules
#   ./scripts/mesh.sh canary 90 10           send 90% of catalog traffic to v1 and 10% to v2
#   ./scripts/mesh.sh retries 1              1 = no retries (default is 3)
#   ./scripts/mesh.sh sample 50              send 50 requests, count which version answered
#   ./scripts/mesh.sh sample 20 x-canary:true    same, with a header (this one forces v2)
#   ./scripts/mesh.sh trace                  look up the newest trace in Jaeger
set -uo pipefail
. "$(dirname "$0")/lib.sh"
case "${1:-}" in
  status)  $MESHCTL status ;;
  canary)  [ $# -eq 3 ] || die "usage: $0 canary V1_PERCENT V2_PERCENT"; $MESHCTL weights "$2" "$3" ;;
  retries) [ $# -eq 2 ] || die "usage: $0 retries N"; $MESHCTL retries "$2" ;;
  sample)  if [ $# -ge 3 ]; then $MESHCTL sample "$2" -H "$3"; else $MESHCTL sample "${2:-50}"; fi ;;
  trace)   $MESHCTL trace ;;
  *)       die "usage: $0 status | canary V1 V2 | retries N | sample N [header:value] | trace" ;;
esac
