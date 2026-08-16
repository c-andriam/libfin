#!/bin/bash
# script: vault_init_prod.sh
# Initializes Vault for production via HTTP API and provisions the Web3 private key.

set -e

# Default to the container name if running inside the network, or localhost if port-forwarded
VAULT_ADDR=${VAULT_ADDR:-"http://127.0.0.1:8200"}

echo "Initializing Vault at $VAULT_ADDR..."

# Initialize Vault
INIT_RESPONSE=$(curl -s -X POST -d '{"secret_shares": 1, "secret_threshold": 1}' $VAULT_ADDR/v1/sys/init)

if echo $INIT_RESPONSE | grep -q "keys"; then
    echo "Vault initialized successfully."
    UNSEAL_KEY=$(echo $INIT_RESPONSE | grep -o '"keys_base64":\["[^"]*' | cut -d '"' -f 4)
    ROOT_TOKEN=$(echo $INIT_RESPONSE | grep -o '"root_token":"[^"]*' | cut -d '"' -f 4)

    echo "=========================================================="
    echo "SAVE THESE CREDENTIALS SECURELY. THEY WILL NOT BE SHOWN AGAIN."
    echo "Unseal Key: $UNSEAL_KEY"
    echo "Root Token: $ROOT_TOKEN"
    echo "=========================================================="

    # Unseal Vault
    curl -s -X POST -d "{\"key\": \"$UNSEAL_KEY\"}" $VAULT_ADDR/v1/sys/unseal > /dev/null
    echo "Vault unsealed."

    # Enable KV v2 engine
    curl -s -X POST -H "X-Vault-Token: $ROOT_TOKEN" \
        -d '{"type": "kv", "options": {"version": "2"}}' \
        $VAULT_ADDR/v1/sys/mounts/secret > /dev/null
    echo "KV v2 secrets engine enabled at 'secret/'"

    # Put a dummy key (Replace manually via UI/CLI in real prod)
    echo "Writing placeholder Web3 private key (UPDATE THIS MANUALLY IN PROD!)..."
    curl -s -X POST -H "X-Vault-Token: $ROOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"data": {"private_key": "YOUR_REAL_PRIVATE_KEY_HERE"}}' \
        $VAULT_ADDR/v1/secret/data/gateway/web3 > /dev/null
    
    echo "Vault production setup complete."
else
    echo "Vault is already initialized or unreachable. (Response: $INIT_RESPONSE)"
    exit 1
fi
