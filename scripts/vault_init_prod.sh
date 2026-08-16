#!/usr/bin/env bash
# ============================================================================
#  Provision the gateway's secrets in a production Vault.
# ============================================================================
#  Assumes Vault is already initialised and unsealed:
#      make prod-vault-init      (once, ever — keep the unseal keys offline)
#      make prod-vault-unseal    (after every restart of the container)
#
#  This script writes two secrets:
#      secret/gateway/web3 :: private_key      the hot wallet
#      secret/gateway/pan  :: encryption_key   protects staged card numbers
#
#  It never invents a private key. The previous version wrote a literal
#  "YOUR_REAL_PRIVATE_KEY_HERE", which would have let the stack start and fail
#  at the first transfer — after a card had already been charged.
#
#  Usage:
#      VAULT_TOKEN=... scripts/vault_init_prod.sh
#      VAULT_TOKEN=... WEB3_PRIVATE_KEY=0x... scripts/vault_init_prod.sh
# ============================================================================
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
VAULT_TOKEN="${VAULT_TOKEN:-}"
CURL_OPTS=(--silent --show-error --max-time 15)

# Verify TLS properly when Vault is reached over HTTPS.
if [[ "${VAULT_ADDR}" == https://* && -n "${VAULT_CACERT:-}" ]]; then
    CURL_OPTS+=(--cacert "${VAULT_CACERT}")
fi

die() { echo "ERROR: $*" >&2; exit 1; }

[[ -n "${VAULT_TOKEN}" ]] || die "VAULT_TOKEN is required. Export the root or an admin token."

echo "Vault at ${VAULT_ADDR}"

# ── 1. Vault must be initialised and unsealed ───────────────────────────────
HEALTH="$(curl "${CURL_OPTS[@]}" "${VAULT_ADDR}/v1/sys/health?standbyok=true" || true)"
[[ -n "${HEALTH}" ]] || die "Vault is unreachable at ${VAULT_ADDR}."

if echo "${HEALTH}" | grep -q '"initialized":false'; then
    die "Vault is not initialised. Run: make prod-vault-init"
fi
if echo "${HEALTH}" | grep -q '"sealed":true'; then
    die "Vault is sealed. Run: make prod-vault-unseal"
fi
echo "  Vault is initialised and unsealed."

# ── 2. The KV v2 engine ─────────────────────────────────────────────────────
MOUNTS="$(curl "${CURL_OPTS[@]}" -H "X-Vault-Token: ${VAULT_TOKEN}" "${VAULT_ADDR}/v1/sys/mounts")"
echo "${MOUNTS}" | grep -q '"errors":\["permission denied"\]' && die "The token was rejected."

if echo "${MOUNTS}" | grep -q '"secret/"'; then
    echo "  KV engine already mounted at secret/."
else
    curl "${CURL_OPTS[@]}" -X POST -H "X-Vault-Token: ${VAULT_TOKEN}" \
        -d '{"type": "kv", "options": {"version": "2"}}' \
        "${VAULT_ADDR}/v1/sys/mounts/secret" >/dev/null
    echo "  Mounted KV v2 at secret/."
fi

write_secret() {
    local path="$1" payload="$2" label="$3"
    local response
    response="$(curl "${CURL_OPTS[@]}" -X POST \
        -H "X-Vault-Token: ${VAULT_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "${payload}" \
        "${VAULT_ADDR}/v1/secret/data/${path}")"
    if echo "${response}" | grep -q '"errors":\[.\+\]'; then
        die "Could not write ${label}: ${response}"
    fi
    echo "  Wrote ${label} to secret/${path}."
}

secret_exists() {
    curl "${CURL_OPTS[@]}" -H "X-Vault-Token: ${VAULT_TOKEN}" \
        "${VAULT_ADDR}/v1/secret/data/$1" 2>/dev/null | grep -q '"data"'
}

# ── 3. The hot wallet key ───────────────────────────────────────────────────
echo
echo "── Hot wallet key ──────────────────────────────────────────"

if secret_exists "gateway/web3"; then
    echo "  secret/gateway/web3 already exists; leaving it untouched."
    echo "  To rotate it, delete the secret in Vault and run this again."
elif [[ -n "${WEB3_PRIVATE_KEY:-}" ]]; then
    write_secret "gateway/web3" \
        "{\"data\": {\"private_key\": \"${WEB3_PRIVATE_KEY}\"}}" \
        "the hot wallet key"
    echo "  Now remove WEB3_PRIVATE_KEY from your shell history and .env.prod."
else
    echo "  No key provided, and none stored."
    echo
    echo "  The gateway cannot send crypto without it. Provide it either by"
    echo "  re-running this script with WEB3_PRIVATE_KEY set, or directly:"
    echo
    echo "      vault kv put secret/gateway/web3 private_key=0x..."
    echo
    echo "  Use a wallet funded only with the float you are willing to expose:"
    echo "  whatever key the gateway holds, the gateway can spend."
fi

# ── 4. The PAN encryption key ───────────────────────────────────────────────
echo
echo "── PAN encryption key ──────────────────────────────────────"

if secret_exists "gateway/pan"; then
    echo "  secret/gateway/pan already exists; leaving it untouched."
    echo "  Rotating this key makes pending reversals unrecoverable — drain the"
    echo "  queue first."
else
    PAN_KEY="${PAN_ENCRYPTION_KEY:-$(openssl rand -base64 32)}"
    write_secret "gateway/pan" \
        "{\"data\": {\"encryption_key\": \"${PAN_KEY}\"}}" \
        "the PAN encryption key"
    echo "  Generated and stored. It protects the card numbers held for the"
    echo "  duration of a possible reversal; without it a failed transfer"
    echo "  cannot be refunded automatically."
fi

echo
echo "Vault provisioning complete."
echo
echo "Next:"
echo "  1. Replace the root token with an AppRole scoped to secret/gateway/*."
echo "  2. Confirm the gateway can read them:  make prod-status"
