#!/usr/bin/env bash
# Make a service misbehave (or heal it) while the demo runs.
#   ./scripts/chaos.sh pricing delay_ms=3000          every request takes 3 seconds
#   ./scripts/chaos.sh pricing error_rate=1 status=500    every request fails with HTTP 500
#   ./scripts/chaos.sh pricing clear                  back to normal
#   ./scripts/chaos.sh all clear                      heal every service
set -uo pipefail
. "$(dirname "$0")/lib.sh"
[ $# -ge 2 ] || die "usage: $0 POD|all key=value ... | clear      (pods: $PODS)"
target="$1"; shift
pods="$target"; [ "$target" = "all" ] && pods="$PODS"
for p in $pods; do case " $PODS " in *" $p "*) ;; *) die "unknown pod '$p' (pods: $PODS)" ;; esac; done
for p in $pods; do
  if [ "$1" = "clear" ]; then
    $DOCKER exec "mesh-$p" python -c "import os; os.path.exists('/tmp/chaos.json') and os.remove('/tmp/chaos.json')" || die "could not reach mesh-$p"
  else
    $DOCKER exec "mesh-$p" python -c "
import json, sys
cfg = {}
for pair in sys.argv[1:]:
    k, v = pair.split('=', 1)
    cfg[k] = float(v) if '.' in v else int(v)
json.dump(cfg, open('/tmp/chaos.json', 'w'))" "$@" || die "could not reach mesh-$p (is the mesh running? ./scripts/up.sh)"
  fi
done
echo "chaos on $target: $*"
