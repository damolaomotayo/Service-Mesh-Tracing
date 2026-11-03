#!/usr/bin/env bash
# Creates the mesh's own certificate authority and one certificate per pod (used for mTLS).
# Each certificate is valid for the pod's name and for both client and server use. Safe to re-run: it does nothing
# if the certificates already exist. Usage: ./scripts/certs.sh [CERT_DIR]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${1:-$ROOT/certs}"
mkdir -p "$DIR"
cd "$DIR"

if [ ! -f ca.pem ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 30 \
    -subj "/CN=mesh-demo-ca" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout ca.key -out ca.pem 2>/dev/null
fi

for pod in $(python3 "$ROOT/mesh/render.py" pods); do
  [ -f "$pod.pem" ] && continue
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj "/CN=$pod" -keyout "$pod.key" -out "$pod.csr" 2>/dev/null
  printf 'subjectAltName=DNS:%s,IP:127.0.0.1\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=CA:FALSE\n' "$pod" > "$pod.ext"
  openssl x509 -req -in "$pod.csr" -CA ca.pem -CAkey ca.key -CAcreateserial -days 30 -extfile "$pod.ext" -out "$pod.pem" 2>/dev/null
  rm -f "$pod.csr" "$pod.ext"
done
chmod 644 ./*.pem ./*.key
echo "certificates ready in $DIR: $(ls ./*.pem | wc -l | tr -d ' ') files"
