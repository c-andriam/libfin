#!/usr/bin/env bash
# ============================================================================
# Vault Initialization Script
# ============================================================================
# This script bootstraps HashiCorp Vault after the container starts.
# It stores the Web3 private key securely and creates an access policy.
#
# Usage:
#   ./scripts/vault_init.sh
#
# Prerequisites:
#   - Vault container must be running and healthy.
#   - Port 8200 must be exposed on the host.
#   - VAULT_TOKEN must be set (or sourced from .env).
# ============================================================================

set -euo pipefail

if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Use localhost since vault port 8200 is mapped to the host
VAULT_ADDR="http://127.0.0.1:8200"
VAULT_TOKEN="${VAULT_TOKEN:-dev-root-token}"

echo "============================================"
echo "  Vault Initialization Script"
echo "  Target: ${VAULT_ADDR}"
echo "============================================"

# Wait for Vault to be ready
echo "[1/5] Waiting for Vault to become available..."
for i in $(seq 1 30); do
    if curl -sf "${VAULT_ADDR}/v1/sys/health" > /dev/null 2>&1; then
        echo "       Vault is ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "       ERROR: Vault did not become available after 30 attempts."
        exit 1
    fi
    echo "       Attempt ${i}/30 - retrying in 2s..."
    sleep 2
done

# ── Step 2: Enable KV v2 secrets engine ────────────────────────────────────
echo "[2/5] Enabling KV v2 secrets engine at 'secret/'..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST \
    -H "X-Vault-Token: ${VAULT_TOKEN}" \
    -d '{"type":"kv","options":{"version":"2"}}' \
    "${VAULT_ADDR}/v1/sys/mounts/secret")

if [ "$HTTP_CODE" = "204" ] || [ "$HTTP_CODE" = "200" ]; then
    echo "       KV v2 engine enabled."
elif [ "$HTTP_CODE" = "400" ]; then
    echo "       KV engine already enabled."
else
    echo "       WARNING: Unexpected response code: ${HTTP_CODE}"
fi

# ── Step 3: Store Web3 private key ─────────────────────────────────────────
echo "[3/5] Storing Web3 private key..."
if [ -z "${WEB3_PRIVATE_KEY:-}" ]; then
    echo "       WARNING: WEB3_PRIVATE_KEY is not set. Storing a placeholder."
    echo "       You MUST update this secret before going to production!"
    WEB3_PRIVATE_KEY="0x0000000000000000000000000000000000000000000000000000000000000001"
fi

curl -sf -X POST \
    -H "X-Vault-Token: ${VAULT_TOKEN}" \
    -d "{\"data\":{\"private_key\":\"${WEB3_PRIVATE_KEY}\",\"rpc_url\":\"${WEB3_RPC_URL:-http://127.0.0.1:8545}\"}}" \
    "${VAULT_ADDR}/v1/secret/data/gateway/web3" > /dev/null

echo "       Secret stored at: secret/gateway/web3"

# ── Step 4: Create gateway policy ──────────────────────────────────────────
echo "[4/5] Creating 'gateway-policy'..."
POLICY_PAYLOAD=$(cat <<'ENDJSON'
{"policy": "path \"secret/data/gateway/*\" { capabilities = [\"read\", \"list\"] }"}
ENDJSON
)

curl -s -X PUT \
    -H "X-Vault-Token: ${VAULT_TOKEN}" \
    -d "${POLICY_PAYLOAD}" \
    "${VAULT_ADDR}/v1/sys/policies/acl/gateway-policy" > /dev/null

echo "       Policy 'gateway-policy' created (read-only on secret/gateway/*)."

# ── Step 5: Generate application token ─────────────────────────────────────
echo "[5/5] Generating application token..."
TOKEN_RESPONSE=$(curl -s -X POST \
    -H "X-Vault-Token: ${VAULT_TOKEN}" \
    -d '{"policies":["gateway-policy"],"ttl":"720h","display_name":"gateway-app"}' \
    "${VAULT_ADDR}/v1/auth/token/create")

APP_TOKEN=$(echo "${TOKEN_RESPONSE}" | python3 -c "import sys,json; print(json.load(sys.stdin)['auth']['client_token'])")

echo ""
echo "============================================"
echo "  VAULT INITIALIZATION COMPLETE"
echo "============================================"
echo ""
echo "  Application Token (use this in .env):"
echo "  VAULT_TOKEN=${APP_TOKEN}"
echo ""
echo "  This token has READ-ONLY access to:"
echo "    secret/data/gateway/*"
echo ""
echo "  TTL: 30 days (renew or recreate before expiry)"
echo "============================================"
