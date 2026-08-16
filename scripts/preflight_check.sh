#!/bin/bash
# script: preflight_check.sh
# Verifies that all production dependencies are reachable and properly configured.

set -e

echo "Starting Production Preflight Check..."

# Check .env.prod exists
if [ ! -f .env.prod ]; then
    echo "❌ ERROR: .env.prod file not found. Copy .env.prod.example to .env.prod and configure it."
    exit 1
fi

source .env.prod

# Check Nginx SSL certs
if [ ! -f nginx/ssl/server.crt ] || [ ! -f nginx/ssl/server.key ]; then
    echo "❌ ERROR: SSL certificates missing in nginx/ssl/. Generate them or mount them."
    exit 1
fi

# Check Vault Token is set
if [ -z "$VAULT_TOKEN" ] || [ "$VAULT_TOKEN" == "REPLACE_ME_VAULT_TOKEN" ]; then
    echo "❌ ERROR: VAULT_TOKEN is not configured in .env.prod."
    exit 1
fi

echo "✅ All local configurations are present."
echo "Note: Full end-to-end connectivity check (DB, Redis, Vault, Bank, Web3) will be performed by the API's /health/ready endpoint on startup."
exit 0
