#!/usr/bin/env bash
# Generate a self-signed TLS certificate for nginx/ssl/.
#
# For simulation, and for a production dry run before the real certificate
# arrives. A self-signed certificate is fine for a stack no browser talks to,
# but preflight warns when production is about to launch with one.
set -euo pipefail

SSL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/nginx/ssl"
COMMON_NAME="${1:-localhost}"
DAYS="${CERT_DAYS:-825}"

mkdir -p "${SSL_DIR}"

if [[ -f "${SSL_DIR}/server.crt" && "${FORCE:-}" != "1" ]]; then
    echo "A certificate already exists at ${SSL_DIR}/server.crt."
    openssl x509 -in "${SSL_DIR}/server.crt" -noout -subject -enddate
    echo "Re-run with FORCE=1 to replace it."
    exit 0
fi

echo "Generating a self-signed certificate for '${COMMON_NAME}' (${DAYS} days)..."

openssl req -x509 -nodes \
    -newkey rsa:4096 \
    -keyout "${SSL_DIR}/server.key" \
    -out "${SSL_DIR}/server.crt" \
    -days "${DAYS}" \
    -subj "/C=US/ST=Simulation/L=Simulation/O=Gateway/CN=${COMMON_NAME}" \
    -addext "subjectAltName=DNS:${COMMON_NAME},DNS:localhost,IP:127.0.0.1" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" \
    2>/dev/null

# The key is mounted into containers; keep it unreadable by anyone else.
chmod 600 "${SSL_DIR}/server.key"
chmod 644 "${SSL_DIR}/server.crt"

echo "Written to ${SSL_DIR}:"
openssl x509 -in "${SSL_DIR}/server.crt" -noout -subject -enddate
echo
echo "Self-signed. For production, replace server.crt/server.key with a"
echo "certificate from a real CA (Let's Encrypt, or your own)."
