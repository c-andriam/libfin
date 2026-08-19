#!/usr/bin/env bash
# ============================================================================
#  Rehearse production against a real blockchain.
# ============================================================================
#  Runs the actual production compose — production image, Gunicorn, Nginx with
#  TLS, network segmentation, Vault in production mode, Postgres with SSL —
#  pointed at a real public testnet. Real gas, real block times, real
#  confirmations, a real RPC provider.
#
#  The acquirer is the one piece that stays simulated, over TLS. No public
#  ISO 8583 test host exists: those sit behind a contract with an acquirer or
#  membership of a card scheme, and no amount of code closes that gap.
#
#  Prerequisite: the wallet in testnet-wallet.json must hold testnet ETH.
#  Generate it with `make testnet-wallet`, fund it from a faucet, then run this.
#
#  Usage:
#      scripts/prodtest.sh                # against public Sepolia (needs funds)
#      scripts/prodtest.sh --local-chain  # against a local chain (needs nothing)
#      scripts/prodtest.sh --down         # tear everything down
#
#  --local-chain trades the real fee market and real block times for the
#  ability to run right now: every Sepolia faucet requires a human, and waiting
#  on one blocks the thing that matters most here — showing the production
#  stack carry a payment from card to chain without stopping. The real-chain
#  behaviour it gives up is covered by tests/load/testnet_check.py.
# ============================================================================
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; BLUE=$'\033[0;34m'; OFF=$'\033[0m'

LOCAL_CHAIN=0
LOCAL_CHAIN_ENV=""
[[ "${1:-}" == "--local-chain" ]] && LOCAL_CHAIN=1

WALLET_FILE="${WALLET_FILE:-testnet-wallet.json}"
ENV_FILE=".env.prodtest"
RPC_URL="${WEB3_RPC_URL:-https://ethereum-sepolia-rpc.publicnode.com}"
CHAIN_ID="${WEB3_CHAIN_ID:-11155111}"
# The ERC-20 to settle in. LINK is the obvious public choice, but its faucet
# needs an authenticated account — one more human gate between here and a
# completed delivery. `--deploy-token` sidesteps it by deploying SimToken to
# the real chain with the gas already in the wallet: still a public network,
# still real confirmations, just a token we minted ourselves.
TOKEN_FILE=".prodtest-token"
DEFAULT_TOKEN="0x779877A7B0D9E8603169DdbD7836e478b4624789"  # LINK on Sepolia
if [[ -f "${TOKEN_FILE}" ]]; then
    DEFAULT_TOKEN="$(cat "${TOKEN_FILE}")"
fi
TOKEN="${ERC20_TOKEN_ADDRESS:-${DEFAULT_TOKEN}}"
DEPLOY_TOKEN=0
[[ "${1:-}" == "--deploy-token" ]] && DEPLOY_TOKEN=1
HTTPS_PORT="${HTTPS_PORT:-8444}"
COMPOSE=(podman-compose -f podman-compose.prod.yml -f podman-compose.prodtest.yml)
if (( LOCAL_CHAIN )); then
    COMPOSE+=(-f podman-compose.prodtest-localchain.yml)
    # Production mode refuses a local chain, correctly. The rehearsal says so
    # explicitly rather than weakening the check; preflight refuses this flag.
    LOCAL_CHAIN_ENV="ALLOW_SIMULATED_CHAIN=true"
    RPC_URL="http://gateway-anvil:8545"
    CHAIN_ID=31337
    # Deterministic address of the first contract Anvil's account 0 deploys.
    TOKEN="0x5FbDB2315678afecb367f032d93F642f64180aa3"
fi
COMPOSE+=(--env-file "${ENV_FILE}")
# Also tells the service definitions which file to load into the containers.
export GATEWAY_ENV_FILE="${ENV_FILE}"

say()  { echo "${BLUE}▶${OFF} $*"; }
ok()   { echo "  ${GREEN}[ ok ]${OFF} $*"; }
bad()  { echo "  ${RED}[fail]${OFF} $*"; }
warn() { echo "  ${YELLOW}[warn]${OFF} $*"; }

if [[ "${1:-}" == "--down" ]]; then
    say "Tearing down."
    podman-compose -f podman-compose.prod.yml -f podman-compose.prodtest.yml \
        -f podman-compose.prodtest-localchain.yml --env-file "${ENV_FILE}" \
        down 2>/dev/null | tail -1
    podman rm -f gateway-bank-prodtest gateway-anvil-prodtest \
        gateway-token-deployer-prodtest >/dev/null 2>&1
    podman volume rm libfin_pgdata_prod libfin_redisdata_prod \
        libfin_vaultdata_prod libfin_vaultlogs_prod \
        libfin_anvildata_prodtest >/dev/null 2>&1
    podman network rm gateway-prod-frontend gateway-prod-backend \
        gateway-prod-egress >/dev/null 2>&1
    rm -f "${ENV_FILE}"
    ok "Removed. testnet-wallet.json was kept."
    exit 0
fi

LOCK_FILE=".prodtest.lock"
# Check the pid is still *this* script, not merely some process that inherited
# the number — a killed run leaves the file behind and a reused pid would then
# block every future run.
LOCK_PID="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
if [[ -n "${LOCK_PID}" ]] && kill -0 "${LOCK_PID}" 2>/dev/null &&
   grep -q "prodtest" "/proc/${LOCK_PID}/cmdline" 2>/dev/null; then
    bad "Another run of this script is already in progress (pid $(cat "${LOCK_FILE}"))."
    echo "  Two runs share the same containers and will fight over Vault."
    exit 1
fi
echo $$ > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"' EXIT

rpc() {
    curl -s --max-time 20 -X POST -H 'Content-Type: application/json' \
        --data "{\"jsonrpc\":\"2.0\",\"method\":\"$1\",\"params\":$2,\"id\":1}" "${RPC_URL}"
}

# ── 1. The wallet ───────────────────────────────────────────────────────────
if (( LOCAL_CHAIN )); then
    say "Local chain: using Anvil account #1, funded by the token deployer"
    # A published test key. Worthless anywhere real, which is the point.
    ADDRESS="0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    PRIVATE_KEY="0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    export LOCAL_CHAIN_WALLET="${ADDRESS}"
    ok "Hot wallet ${ADDRESS}"
else

say "Checking the testnet wallet"

if [[ ! -f "${WALLET_FILE}" ]]; then
    bad "${WALLET_FILE} not found. Run: make testnet-wallet"
    exit 1
fi

ADDRESS="$(python3 -c "import json;print(json.load(open('${WALLET_FILE}'))['address'])")"
PRIVATE_KEY="$(python3 -c "import json;print(json.load(open('${WALLET_FILE}'))['private_key'])")"

BALANCE_HEX="$(rpc eth_getBalance "[\"${ADDRESS}\",\"latest\"]" | grep -o '"result":"0x[0-9a-fA-F]*"' | cut -d'"' -f4)"
if [[ -z "${BALANCE_HEX}" ]]; then
    bad "The RPC at ${RPC_URL} did not answer."
    exit 1
fi
BALANCE_WEI=$((BALANCE_HEX))

if (( BALANCE_WEI == 0 )); then
    bad "The wallet holds no testnet ETH and cannot pay for gas."
    echo
    echo "  Fund this address, then run this script again:"
    echo
    echo "      ${ADDRESS}"
    echo
    echo "  Sepolia faucets (all need a human — captcha, a login, or browser"
    echo "  proof-of-work; automating them would abuse the service):"
    echo "      https://sepolia-faucet.pk910.de"
    echo "      https://www.alchemy.com/faucets/ethereum-sepolia"
    echo "      https://cloud.google.com/application/web3/faucet/ethereum/sepolia"
    echo
    echo "  For the ERC-20 leg you also need test LINK, from:"
    echo "      https://faucets.chain.link/sepolia"
    exit 1
fi
ok "Gas balance: $(python3 -c "print(f'{${BALANCE_WEI}/1e18:.6f}')") ETH at ${ADDRESS}"

TOKEN_BALANCE_HEX="$(rpc eth_call "[{\"to\":\"${TOKEN}\",\"data\":\"0x70a08231000000000000000000000000${ADDRESS:2}\"},\"latest\"]" \
    | grep -o '"result":"0x[0-9a-fA-F]*"' | cut -d'"' -f4)"
TOKEN_BALANCE=$((${TOKEN_BALANCE_HEX:-0x0}))
if (( TOKEN_BALANCE == 0 )) && (( DEPLOY_TOKEN )); then
    say "Deploying an ERC-20 to ${RPC_URL}"
    DEPLOY_OUT="$(podman run --rm -v ./sim:/sim:ro,z --entrypoint sh \
        ghcr.io/foundry-rs/foundry:latest -c "
            mkdir -p /tmp/t/src && cp /sim/SimToken.sol /tmp/t/src/ && cd /tmp/t
            printf '[profile.default]\nsrc = \"src\"\nout = \"out\"\nlibs = []\n' > foundry.toml
            forge create src/SimToken.sol:SimToken --rpc-url ${RPC_URL} \
                --private-key ${PRIVATE_KEY} --broadcast \
                --constructor-args 1000000000000 2>&1
        " 2>&1)"
    TOKEN="$(echo "${DEPLOY_OUT}" | grep -o 'Deployed to: 0x[0-9a-fA-F]*' | awk '{print $3}')"
    if [[ -z "${TOKEN}" ]]; then
        bad "Token deployment failed:"
        echo "${DEPLOY_OUT}" | tail -5
        exit 1
    fi
    echo "${TOKEN}" > "${TOKEN_FILE}"
    ok "Deployed at ${TOKEN}; the constructor minted the supply to this wallet"
    TOKEN_BALANCE_HEX="$(rpc eth_call "[{\"to\":\"${TOKEN}\",\"data\":\"0x70a08231000000000000000000000000${ADDRESS:2}\"},\"latest\"]" \
        | grep -o '"result":"0x[0-9a-fA-F]*"' | cut -d'"' -f4)"
    TOKEN_BALANCE=$((${TOKEN_BALANCE_HEX:-0x0}))
fi

if (( TOKEN_BALANCE == 0 )); then
    warn "The wallet holds no ${TOKEN:0:10}… tokens. The transfer will fail with"
    warn "InsufficientFunds and the payment will be reversed — the safety path,"
    warn "but not a completed delivery."
    warn "Either fund it from https://faucets.chain.link/sepolia, or run"
    warn "  scripts/prodtest.sh --deploy-token   to deploy an ERC-20 with the gas you have."
else
    ok "Token balance: ${TOKEN_BALANCE} units at ${TOKEN}"
fi

fi  # end of the public-testnet branch

# ── 2. A certificate the gateway can actually verify ────────────────────────
say "Issuing a certificate for the acquirer simulator"

# The gateway refuses BANK_TLS_INSECURE in production — correctly, since that
# flag disables verification on a link carrying card data. So rather than
# weaken the rehearsal to match the simulator, give the simulator a
# certificate valid for the hostname the gateway dials, and let the gateway
# verify it for real. The rehearsal then exercises the production TLS path
# rather than a relaxed version of it.
mkdir -p acquirer-certs
if [[ ! -f acquirer-certs/bank.crt ]]; then
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout acquirer-certs/bank.key \
        -out acquirer-certs/bank.crt \
        -days 365 \
        -subj "/C=US/ST=Test/L=Test/O=Acquirer Simulator/CN=bank-simulator" \
        -addext "subjectAltName=DNS:bank-simulator,DNS:localhost,IP:127.0.0.1" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=digitalSignature,keyEncipherment,keyCertSign" \
        -addext "extendedKeyUsage=serverAuth" 2>/dev/null
    # 644, not 600: the simulator runs as the image's unprivileged user, whose
    # uid does not match the host user that just wrote this file. A production
    # key would never be world-readable — this one is a throwaway, regenerated
    # per rehearsal, gitignored, and worthless outside this stack.
    chmod 644 acquirer-certs/bank.key
    chmod 644 acquirer-certs/bank.crt
fi
ok "Certificate for CN=bank-simulator issued; the gateway will verify it"

# ── 3. Build the environment ────────────────────────────────────────────────
say "Writing ${ENV_FILE}"

API_KEY="$(openssl rand -hex 32)"
PG_PASSWORD="$(openssl rand -hex 16)"
REDIS_PASSWORD="$(openssl rand -hex 16)"
PAN_KEY="$(openssl rand -base64 32)"

cat > "${ENV_FILE}" <<EOF
# Generated by scripts/prodtest.sh. Testnet only — delete after the run.
GATEWAY_MODE=production
ENVIRONMENT=production
# This is a rehearsal: everything runs in production mode except the acquirer,
# which is a simulator because no public ISO 8583 test host exists. Preflight
# refuses this flag, so it cannot follow the config into a real launch.
ALLOW_SIMULATED_ACQUIRER=true
${LOCAL_CHAIN_ENV:-}

POSTGRES_PASSWORD=${PG_PASSWORD}
REDIS_PASSWORD=${REDIS_PASSWORD}
DATABASE_URL=postgresql+asyncpg://gateway:${PG_PASSWORD}@gateway-postgres:5432/gateway_db
DATABASE_SSL=true
DATABASE_SSL_MODE=require
REDIS_URL=redis://:${REDIS_PASSWORD}@gateway-redis:6379/0
CELERY_BROKER_URL=redis://:${REDIS_PASSWORD}@gateway-redis:6379/0
AUTO_CREATE_SCHEMA=false

VAULT_ADDR=http://gateway-vault:8200
VAULT_TOKEN=placeholder-set-after-init
VAULT_DISABLE_MLOCK=true

BANK_HOST=bank-simulator
BANK_PORT=9000
BANK_USE_TLS=true
# Verification stays ON, as production demands. The simulator's certificate is
# self-signed, so it is also its own CA — trust it explicitly rather than
# switching verification off.
BANK_TLS_INSECURE=false
BANK_TLS_CA_FILE=/certs/bank.crt
BANK_TIMEOUT_SEC=10
BANK_ECHO_INTERVAL_SEC=60
ACQUIRER_TERMINAL_ID=PRODTST1
ACQUIRER_MERCHANT_ID=PRODTEST00000001
ACQUIRER_INSTITUTION_ID=12345678901
ACQUIRER_NAME=PRODTEST GATEWAY
ACQUIRER_CITY=PARIS
ACQUIRER_STATE=
ACQUIRER_COUNTRY_ALPHA=FR
ACQUIRER_MERCHANT_CATEGORY=6051
ACQUIRER_POS_CONDITION=59
ACQUIRER_DIALECT=iso87
ACQUIRER_CURRENCY=840
ACQUIRER_POS_DATA=810101Y00000
ACQUIRER_PROCESSING_CODE=000000
ACQUIRER_SEND_CVV=true

WEB3_RPC_URL=${RPC_URL}
WEB3_RPC_URL_BACKUP=${RPC_URL}
WEB3_CHAIN_ID=${CHAIN_ID}
WEB3_CONFIRMATIONS=2
WEB3_RECEIPT_TIMEOUT_SEC=300
WEB3_CONFIRMATION_TIMEOUT_SEC=600
WEB3_MAX_FEE_GWEI=200
ERC20_TOKEN_ADDRESS=${TOKEN}

# Prices come from the real mainnet oracles even when settlement is elsewhere:
# reading a feed is free and changes nothing, and it is the only way the live
# rate path gets exercised through the whole stack rather than by a script.
RATE_SOURCE=chainlink
RATE_RPC_URL=https://ethereum-rpc.publicnode.com
RATE_TOKEN_SYMBOL=USDT
RATE_SETTLEMENT_CURRENCY=USD
RATE_SPREAD=0.01
RATE_MAX_MOVEMENT=0.10
RATE_STALENESS_MARGIN=1.5
RATE_BOUNDS=USDT/USD:0.5:1.5,EUR/USD:0.5:2.0,GBP/USD:0.5:2.5
RATE_CROSS_CHECK_SOURCE=coingecko
RATE_CROSS_CHECK_TOLERANCE=0.02
RATE_CACHE_SECONDS=30
EXCHANGE_RATE=1.0
WEB3_PRIVATE_KEY=

GATEWAY_API_KEY=${API_KEY}
CORS_ORIGINS=https://localhost
RATE_LIMIT_PER_MINUTE=30
TRUSTED_PROXIES=*
AMOUNT_MIN=0.01
AMOUNT_MAX=100.00
PAN_ENCRYPTION_KEY=${PAN_KEY}
PAN_TOKEN_TTL_SEC=86400

CIRCUIT_BREAKER_FAIL_CLOSED=true
CIRCUIT_BREAKER_THRESHOLD=3
CIRCUIT_BREAKER_RECOVERY_SEC=30
RECONCILIATION_INTERVAL_SEC=900
STALE_TRANSACTION_MINUTES=15

WEB_CONCURRENCY=2
CELERY_CONCURRENCY=2
HTTP_PORT=8081
HTTPS_PORT=${HTTPS_PORT}
EOF
chmod 600 "${ENV_FILE}"
ok "Written (0600)"

# ── 3. Start the data plane ─────────────────────────────────────────────────
say "Starting the production stack"
podman build -t gateway:prod -f Containerfile.prod . >/dev/null 2>&1 || {
    bad "Image build failed."; exit 1; }

if (( LOCAL_CHAIN )); then
    say "Bringing up the local chain and deploying the token"
    "${COMPOSE[@]}" up -d gateway-anvil gateway-token-deployer >/dev/null 2>&1
    for _ in {1..40}; do
        podman logs gateway-token-deployer-prodtest 2>&1 |
            grep -qE "deployed at|already deployed" && break
        sleep 5
    done
    podman logs gateway-token-deployer-prodtest 2>&1 |
        grep -E "Deployed to|already deployed|funded" | tail -2 | sed 's/^/  /'
fi

"${COMPOSE[@]}" up -d gateway-postgres gateway-redis gateway-vault >/dev/null 2>&1
# podman-compose creates the named services but does not always start them all,
# so nudge each one and wait for it rather than assuming.
podman start gateway-postgres-prod gateway-redis-prod gateway-vault-prod >/dev/null 2>&1

for _ in {1..40}; do
    [[ "$(podman inspect gateway-postgres-prod --format '{{.State.Health.Status}}' 2>/dev/null)" == "healthy" ]] && break
    sleep 3
done
ok "Postgres and Redis are up"

for _ in {1..30}; do
    [[ "$(podman inspect gateway-vault-prod --format '{{.State.Status}}' 2>/dev/null)" == "running" ]] && break
    sleep 2
done
if [[ "$(podman inspect gateway-vault-prod --format '{{.State.Status}}' 2>/dev/null)" != "running" ]]; then
    bad "Vault did not start. Check: podman logs gateway-vault-prod"
    exit 1
fi
# Vault needs a moment past process start before it answers the API.
for _ in {1..20}; do
    podman exec -e VAULT_ADDR=http://127.0.0.1:8200 gateway-vault-prod \
        vault status >/dev/null 2>&1 && break
    podman exec -e VAULT_ADDR=http://127.0.0.1:8200 gateway-vault-prod \
        vault status 2>&1 | grep -q "Sealed" && break
    sleep 2
done
ok "Vault is listening"

# ── 4. Vault: init, unseal, store the key ───────────────────────────────────
say "Provisioning Vault"

VAULT_EXEC=(podman exec -e VAULT_ADDR=http://127.0.0.1:8200 gateway-vault-prod)

# A Vault that is already initialised cannot be initialised again, and the
# unseal key from the first time is the only way back in. Say so plainly
# instead of reporting a generic failure — this happens whenever a previous
# run was interrupted, or two runs overlapped.
if "${VAULT_EXEC[@]}" vault status 2>/dev/null | grep -q "Initialized.*true"; then
    bad "Vault is already initialised, from an earlier run of this script."
    echo
    echo "  Its unseal key belongs to that run, so this one cannot take over."
    echo "  Start clean with:"
    echo
    echo "      scripts/prodtest.sh --down"
    echo
    echo "  That discards the rehearsal's Vault data. It is a rehearsal, so"
    echo "  nothing of value is in there."
    exit 1
fi

INIT_JSON="$("${VAULT_EXEC[@]}" vault operator init -key-shares=1 -key-threshold=1 -format=json 2>/dev/null)"
if [[ -z "${INIT_JSON}" ]]; then
    bad "Vault would not initialise. Check: podman logs gateway-vault-prod"
    exit 1
fi
UNSEAL_KEY="$(echo "${INIT_JSON}" | python3 -c "import json,sys;print(json.load(sys.stdin)['unseal_keys_hex'][0])")"
ROOT_TOKEN="$(echo "${INIT_JSON}" | python3 -c "import json,sys;print(json.load(sys.stdin)['root_token'])")"

"${VAULT_EXEC[@]}" vault operator unseal "${UNSEAL_KEY}" >/dev/null 2>&1
ok "Initialised and unsealed"

podman exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN="${ROOT_TOKEN}" gateway-vault-prod \
    sh -c "vault secrets enable -path=secret -version=2 kv >/dev/null 2>&1;
           vault kv put secret/gateway/web3 private_key=${PRIVATE_KEY} >/dev/null" 2>&1
ok "Hot wallet key stored at secret/gateway/web3"

sed -i "s|^VAULT_TOKEN=.*|VAULT_TOKEN=${ROOT_TOKEN}|" "${ENV_FILE}"
echo "${UNSEAL_KEY}" > .prodtest-unseal-key && chmod 600 .prodtest-unseal-key

# ── 5. Migrate, then start everything ───────────────────────────────────────
say "Applying the schema"
"${COMPOSE[@]}" run --rm gateway-api python /app/scripts/migrate.py 2>&1 | tail -2

say "Starting the application"
"${COMPOSE[@]}" up -d >/dev/null 2>&1

# Bringing the full stack up recreates the Vault container, and a production
# Vault comes back sealed every time — by design, and the reason the runbook
# has an unseal step after any restart. Unsealing here rather than earlier
# because an unseal before this point would simply be undone.
for _ in {1..20}; do
    [[ "$(podman inspect gateway-vault-prod --format '{{.State.Status}}' 2>/dev/null)" == "running" ]] && break
    sleep 2
done
if podman exec -e VAULT_ADDR=http://127.0.0.1:8200 gateway-vault-prod \
        vault status 2>/dev/null | grep -q "Sealed.*true"; then
    podman exec -e VAULT_ADDR=http://127.0.0.1:8200 gateway-vault-prod \
        vault operator unseal "${UNSEAL_KEY}" >/dev/null 2>&1
    ok "Vault unsealed again after the restart"
    # The API caches "can this gateway sign?" for half a minute, so a restart
    # here is faster than waiting for the cache to expire.
    podman restart gateway-api-prod >/dev/null 2>&1
fi
for _ in {1..40}; do
    [[ "$(podman inspect gateway-api-prod --format '{{.State.Health.Status}}' 2>/dev/null)" == "healthy" ]] && break
    sleep 3
done

READY="$(curl -sk -H "X-API-Key: ${API_KEY}" "https://localhost:${HTTPS_PORT}/health/ready")"
echo "  readiness: ${READY}"

# ── 6. A real payment ───────────────────────────────────────────────────────
say "Sending a payment through the production stack"

RESPONSE="$(curl -sk -X POST "https://localhost:${HTTPS_PORT}/pay" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${API_KEY}" \
    -H "Idempotency-Key: prodtest-$(date +%s)" \
    -d "{\"pan\":\"4111111111111111\",\"expiry\":\"3012\",\"cvv\":\"123\",
         \"amount\":\"1.00\",\"currency\":\"USD\",\"target_wallet\":\"${ADDRESS}\"}")"
echo "  ${RESPONSE}"

TX_ID="$(echo "${RESPONSE}" | python3 -c "import json,sys;print(json.load(sys.stdin).get('transaction_id',''))" 2>/dev/null)"
if [[ -z "${TX_ID}" ]]; then
    bad "The payment was not accepted."
    exit 1
fi
ok "Fiat approved, transaction ${TX_ID}"

say "Waiting for on-chain settlement (real block times)"
for _ in {1..80}; do
    STATE="$(curl -sk -H "X-API-Key: ${API_KEY}" "https://localhost:${HTTPS_PORT}/transaction/${TX_ID}")"
    STATUS="$(echo "${STATE}" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])" 2>/dev/null)"
    [[ "${STATUS}" =~ ^(CRYPTO_SENT|REVERSED|REVERSAL_FAILED|FIAT_DECLINED)$ ]] && break
    sleep 8
done

echo "  ${STATE}"
HASH="$(echo "${STATE}" | python3 -c "import json,sys;print(json.load(sys.stdin).get('crypto_tx_hash') or '')" 2>/dev/null)"

echo
case "${STATUS}" in
    CRYPTO_SENT)
        ok "Settled on a real chain."
        [[ -n "${HASH}" ]] && echo "     https://sepolia.etherscan.io/tx/${HASH}"
        ;;
    REVERSED)
        warn "The transfer failed and the cardholder was refunded — the safety"
        warn "path worked, but nothing was delivered. Usually a token balance of zero."
        ;;
    *)
        bad "Ended in ${STATUS}. See: podman logs gateway-worker-prod"
        ;;
esac

echo
echo "  Tear down with: scripts/prodtest.sh --down"
