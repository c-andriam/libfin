#!/usr/bin/env bash
# ============================================================================
#  Production preflight.
# ============================================================================
#  The last gate before real cards are charged. It answers one question: is
#  anything about this deployment still wearing simulation clothes?
#
#  Three classes of check:
#    BLOCKERS  — a launch here would take money it cannot honour. Exit 1.
#    WARNINGS  — works, but you should know. Exit 0.
#    INFO      — what the stack will actually do.
#
#  Usage:
#      scripts/preflight_check.sh              # full check against .env.prod
#      ENV_FILE=.env.staging scripts/...       # another environment file
#      SKIP_NETWORK=1 scripts/...              # config only, no connectivity
# ============================================================================
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

ENV_FILE="${ENV_FILE:-.env.prod}"
SKIP_NETWORK="${SKIP_NETWORK:-0}"

BLOCKERS=0
WARNINGS=0

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; OFF=$'\033[0m'

block()  { echo "${RED}  [BLOCK]${OFF} $*";  BLOCKERS=$((BLOCKERS + 1)); }
warn()   { echo "${YELLOW}  [WARN ]${OFF} $*"; WARNINGS=$((WARNINGS + 1)); }
pass()   { echo "${GREEN}  [ ok  ]${OFF} $*"; }
info()   { echo "${BLUE}  [info ]${OFF} $*"; }
section(){ echo; echo "── $* ─────────────────────────────────────────" ; }

echo "============================================================"
echo " Production preflight — ${ENV_FILE}"
echo "============================================================"

# ── 1. The environment file ─────────────────────────────────────────────────
section "Environment file"

if [[ ! -f "${ENV_FILE}" ]]; then
    block "${ENV_FILE} not found. Start from: cp .env.prod.example ${ENV_FILE}"
    echo
    echo "${RED}Preflight failed: nothing to check.${OFF}"
    exit 1
fi
pass "${ENV_FILE} exists."

PERMS="$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || echo '???')"
if [[ "${PERMS}" != "600" && "${PERMS}" != "400" ]]; then
    warn "${ENV_FILE} is mode ${PERMS}; it holds secrets. Run: chmod 600 ${ENV_FILE}"
else
    pass "${ENV_FILE} permissions are ${PERMS}."
fi

# Load it. `set -a` exports everything so the Python checks below inherit it.
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}" || { block "Could not parse ${ENV_FILE}."; exit 1; }
set +a

# ── 2. Placeholders ─────────────────────────────────────────────────────────
section "Unfilled values"

PLACEHOLDERS="$(grep -nE '^[A-Z_]+=.*REPLACE_ME' "${ENV_FILE}" || true)"
# When the fiat leg is the ISO 8583 link, the PayMeGate block is inert: its
# REPLACE_ME placeholders would otherwise block a launch that does not use it.
if [[ "${ACQUIRER:-iso8583}" != "paymegate" ]]; then
    PLACEHOLDERS="$(printf '%s\n' "${PLACEHOLDERS}" | grep -vE '^[0-9]+:PAYMEGATE_' || true)"
fi
if [[ -n "${PLACEHOLDERS}" ]]; then
    while IFS= read -r line; do
        block "Still a placeholder: ${line%%=*}"
    done <<< "${PLACEHOLDERS}"
else
    pass "No REPLACE_ME values remain."
fi

# ── 3. Simulation leftovers ─────────────────────────────────────────────────
section "Simulation leftovers"

# Anvil's first two published private keys. Anyone can spend from them.
KNOWN_TEST_KEYS=(
    "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    "59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
)
for key in "${KNOWN_TEST_KEYS[@]}"; do
    if grep -qi "${key}" "${ENV_FILE}"; then
        block "A published Anvil test key is present. Anyone can drain that wallet."
    fi
done

case "${WEB3_CHAIN_ID:-}" in
    31337|1337) block "WEB3_CHAIN_ID=${WEB3_CHAIN_ID} is a local test chain." ;;
    "")         block "WEB3_CHAIN_ID is unset. Set it so a wrong RPC is caught before signing." ;;
    *)          pass  "WEB3_CHAIN_ID=${WEB3_CHAIN_ID}." ;;
esac

if [[ "${BANK_HOST:-}" =~ ^(bank-simulator|localhost|127\.0\.0\.1)$ ]]; then
    block "BANK_HOST=${BANK_HOST} is the simulator, not an acquirer."
else
    pass "BANK_HOST=${BANK_HOST:-<unset>}."
fi

if [[ "${WEB3_RPC_URL:-}" =~ (anvil|hardhat|127\.0\.0\.1|localhost) ]]; then
    block "WEB3_RPC_URL points at a local chain."
fi

if grep -qiE '^(GATEWAY_API_KEY|PAN_ENCRYPTION_KEY|POSTGRES_PASSWORD|REDIS_PASSWORD)=.*(simulation|test|changeme|password|secret)$' "${ENV_FILE}"; then
    block "A credential still has a placeholder-looking value."
fi

# ── 4. Security posture ─────────────────────────────────────────────────────
section "Security posture"

if [[ "${ALLOW_SIMULATED_ACQUIRER:-false}" == "true" ]]; then
    block "ALLOW_SIMULATED_ACQUIRER=true. That flag exists only for rehearsals \
against the simulator; a system serving real cardholders must never carry it. Remove it."
else
    pass "No simulated-acquirer escape hatch is set."
fi

if [[ "${ALLOW_SIMULATED_CHAIN:-false}" == "true" ]]; then
    block "ALLOW_SIMULATED_CHAIN=true. That flag lets production settle on a local \
test network, where no value actually moves. It exists for rehearsals only. Remove it."
else
    pass "No simulated-chain escape hatch is set."
fi

[[ "${GATEWAY_MODE:-}" == "production" ]] \
    && pass "GATEWAY_MODE=production." \
    || block "GATEWAY_MODE must be 'production' (currently '${GATEWAY_MODE:-unset}')."

[[ "${BANK_USE_TLS:-}" == "true" ]] \
    && pass "The acquirer link uses TLS." \
    || block "BANK_USE_TLS must be true: the link carries card data."

[[ "${BANK_TLS_INSECURE:-false}" == "false" ]] \
    && pass "Acquirer certificate verification is on." \
    || block "BANK_TLS_INSECURE=true disables certificate verification."

[[ "${CORS_ORIGINS:-}" == "*" ]] \
    && block "CORS_ORIGINS is '*'. Name your front-end origins." \
    || pass "CORS_ORIGINS=${CORS_ORIGINS:-<unset>}."

[[ "${AUTO_CREATE_SCHEMA:-false}" == "false" ]] \
    && pass "Schema auto-creation is off (migrations only)." \
    || block "AUTO_CREATE_SCHEMA must be false in production."

[[ "${CIRCUIT_BREAKER_FAIL_CLOSED:-true}" == "true" ]] \
    && pass "The circuit breaker fails closed." \
    || block "CIRCUIT_BREAKER_FAIL_CLOSED must be true: otherwise a Redis outage lets every payment through."

API_KEY_LEN=${#GATEWAY_API_KEY}
if (( API_KEY_LEN < 32 )); then
    block "GATEWAY_API_KEY is ${API_KEY_LEN} characters. Use at least 32: openssl rand -hex 32"
else
    pass "GATEWAY_API_KEY is ${API_KEY_LEN} characters."
fi

if [[ -z "${PAN_ENCRYPTION_KEY:-}" ]]; then
    block "PAN_ENCRYPTION_KEY is unset: a failed transfer could not be reversed, and the cardholder would keep the debit."
else
    pass "PAN_ENCRYPTION_KEY is set."
fi

[[ -n "${ACQUIRER_TERMINAL_ID:-}" && -n "${ACQUIRER_MERCHANT_ID:-}" ]] \
    && pass "Card acceptor identifiers are set." \
    || block "ACQUIRER_TERMINAL_ID and ACQUIRER_MERCHANT_ID are required; an acquirer declines a 0200 without them."

(( ${WEB3_CONFIRMATIONS:-0} >= 1 )) \
    && pass "WEB3_CONFIRMATIONS=${WEB3_CONFIRMATIONS}." \
    || block "WEB3_CONFIRMATIONS must be at least 1."

[[ -n "${WEB3_RPC_URL_BACKUP:-}" ]] \
    && pass "A backup RPC is configured." \
    || warn "No WEB3_RPC_URL_BACKUP: a single provider outage stops all settlement."

[[ -n "${WEB3_PRIVATE_KEY:-}" ]] \
    && warn "WEB3_PRIVATE_KEY is in ${ENV_FILE}. Prefer Vault at secret/gateway/web3 and leave it blank." \
    || pass "The hot wallet key is not in the environment file."

# ── 5. TLS certificates ─────────────────────────────────────────────────────
section "TLS certificates"

CRT="nginx/ssl/server.crt"
KEY="nginx/ssl/server.key"

if [[ ! -f "${CRT}" || ! -f "${KEY}" ]]; then
    block "Missing ${CRT} or ${KEY}. Generate with: make certs"
else
    pass "Certificate and key are present."

    if ! openssl x509 -in "${CRT}" -noout >/dev/null 2>&1; then
        block "${CRT} is not a readable certificate."
    else
        # The key must actually match the certificate, or nginx fails to start.
        CRT_MOD="$(openssl x509 -noout -modulus -in "${CRT}" 2>/dev/null | openssl md5)"
        KEY_MOD="$(openssl rsa  -noout -modulus -in "${KEY}" 2>/dev/null | openssl md5)"
        if [[ -n "${CRT_MOD}" && "${CRT_MOD}" == "${KEY_MOD}" ]]; then
            pass "The private key matches the certificate."
        else
            block "The private key does not match the certificate."
        fi

        if openssl x509 -in "${CRT}" -noout -checkend 0 >/dev/null 2>&1; then
            if openssl x509 -in "${CRT}" -noout -checkend 2592000 >/dev/null 2>&1; then
                pass "The certificate is valid for at least 30 more days."
            else
                warn "The certificate expires within 30 days: $(openssl x509 -in "${CRT}" -noout -enddate | cut -d= -f2)"
            fi
        else
            block "The certificate has expired."
        fi

        ISSUER="$(openssl x509 -in "${CRT}" -noout -issuer)"
        SUBJECT="$(openssl x509 -in "${CRT}" -noout -subject)"
        if [[ "${ISSUER#issuer=}" == "${SUBJECT#subject=}" ]]; then
            warn "The certificate is self-signed. Clients will reject it unless they pin it."
        else
            pass "The certificate is CA-issued."
        fi
    fi

    KEY_PERMS="$(stat -c '%a' "${KEY}" 2>/dev/null || echo '???')"
    [[ "${KEY_PERMS}" == "600" || "${KEY_PERMS}" == "400" ]] \
        && pass "The private key is mode ${KEY_PERMS}." \
        || warn "The private key is mode ${KEY_PERMS}; run: chmod 600 ${KEY}"
fi

# ── 6. The application's own view ───────────────────────────────────────────
section "Application configuration"

if command -v python3 >/dev/null 2>&1; then
    CONFIG_OUTPUT="$(PYTHONPATH=src python3 - <<'PY' 2>&1
import sys
try:
    from gateway.config import settings
except Exception as exc:
    print(f"IMPORT_ERROR:{exc}")
    sys.exit(0)
for problem in settings.validate():
    print(f"PROBLEM:{problem}")
print(f"SUMMARY:{settings.summary()}")
PY
)"
    while IFS= read -r line; do
        case "${line}" in
            PROBLEM:*)      block "${line#PROBLEM:}" ;;
            IMPORT_ERROR:*) warn  "Could not load the gateway config here: ${line#IMPORT_ERROR:}" ;;
            SUMMARY:*)      info  "${line#SUMMARY:}" ;;
        esac
    done <<< "${CONFIG_OUTPUT}"
    [[ "${CONFIG_OUTPUT}" != *PROBLEM:* && "${CONFIG_OUTPUT}" != *IMPORT_ERROR:* ]] \
        && pass "The application accepts this configuration."
else
    warn "python3 not found; skipped the application's own validation."
fi

# ── 6b. Exchange rates ──────────────────────────────────────────────────────
section "Exchange rates"

if [[ "${RATE_SOURCE:-fixed}" == "fixed" ]]; then
    warn "RATE_SOURCE=fixed uses the constant EXCHANGE_RATE=${EXCHANGE_RATE:-?}. \
Correct only for a stablecoin at parity; anything else silently over- or \
under-delivers on every transaction."
else
    pass "RATE_SOURCE=${RATE_SOURCE}."

    if [[ "${SKIP_NETWORK}" == "1" ]]; then
        info "Feed readability not checked (SKIP_NETWORK=1)."
    else
        # Read every feed the configured currencies need, through the same code
        # the gateway uses. A rate source that cannot be read is a gateway that
        # refuses every payment — better found here than by the first customer.
        RATE_OUTPUT="$(PYTHONPATH=src python3 - <<'RATECHECK' 2>&1
import asyncio, os, sys
try:
    from web3 import Web3
    from gateway.config import settings
    from gateway.currency import SUPPORTED
    from gateway.exchange_rate import apply_spread, build_rate_source
except Exception as exc:
    print(f"SKIP:{exc}")
    sys.exit(0)

url = settings.rate_rpc_url or settings.web3_rpc_urls[0]
try:
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
    source = build_rate_source(w3)
except Exception as exc:
    print(f"FAIL:could not build the rate source: {exc}")
    sys.exit(0)

async def main():
    for alpha in sorted(SUPPORTED):
        pair = f"{alpha}/{settings.rate_token_symbol}"
        try:
            quote = apply_spread(await source.quote(pair))
            hours = quote.age_seconds // 3600
            print(f"OK:{pair}:{quote.rate}:{hours}")
        except Exception as exc:
            print(f"PAIR_FAIL:{pair}:{exc}")

asyncio.run(main())
RATECHECK
)"
        while IFS= read -r line; do
            case "${line}" in
                OK:*)
                    IFS=: read -r _ pair rate hours <<< "${line}"
                    pass "${pair} = ${rate} (source last published ${hours}h ago)"
                    ;;
                PAIR_FAIL:*)
                    detail="${line#PAIR_FAIL:}"
                    warn "Cannot price ${detail%%:*}: ${detail#*:}. Payments in that \
currency will be refused."
                    ;;
                FAIL:*)   block "Exchange rates unavailable: ${line#FAIL:}" ;;
                SKIP:*)   info  "Rate check skipped here: ${line#SKIP:}" ;;
            esac
        done <<< "${RATE_OUTPUT}"
    fi
fi

if [[ -n "${RATE_RPC_URL:-}" ]]; then
    pass "Prices are read over a separate RPC; a rate lookup cannot touch the signing key."
else
    info "Prices are read over the signing RPC. Set RATE_RPC_URL to separate them."
fi

# ── 7. Connectivity ─────────────────────────────────────────────────────────
section "Connectivity"

if [[ "${SKIP_NETWORK}" == "1" ]]; then
    info "Skipped (SKIP_NETWORK=1)."
else
    check_tcp() {
        local name="$1" host="$2" port="$3"
        if [[ -z "${host}" || -z "${port}" ]]; then
            warn "${name}: host or port not configured; skipped."
            return
        fi
        if timeout 5 bash -c "echo > /dev/tcp/${host}/${port}" 2>/dev/null; then
            pass "${name} reachable at ${host}:${port}."
        else
            # Container names only resolve once the stack is up; that is
            # expected before the first launch, hence a warning.
            warn "${name} not reachable at ${host}:${port} from here."
        fi
    }

    check_tcp "Acquirer" "${BANK_HOST:-}" "${BANK_PORT:-}"

    if [[ -n "${WEB3_RPC_URL:-}" ]] && command -v curl >/dev/null 2>&1; then
        RPC_CHAIN="$(curl -s --max-time 10 -X POST \
            -H 'Content-Type: application/json' \
            --data '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}' \
            "${WEB3_RPC_URL}" 2>/dev/null | grep -o '"result":"0x[0-9a-fA-F]*"' | cut -d'"' -f4)"
        if [[ -n "${RPC_CHAIN}" ]]; then
            RPC_CHAIN_DEC=$((RPC_CHAIN))
            if [[ "${RPC_CHAIN_DEC}" == "${WEB3_CHAIN_ID:-}" ]]; then
                pass "The RPC responds and reports chain ${RPC_CHAIN_DEC}, as configured."
            else
                block "The RPC reports chain ${RPC_CHAIN_DEC}, but WEB3_CHAIN_ID is ${WEB3_CHAIN_ID:-unset}."
            fi
        else
            warn "The Web3 RPC did not answer eth_chainId. Check the URL and the API key."
        fi

        if [[ -n "${WEB3_RPC_URL_BACKUP:-}" ]]; then
            if curl -s --max-time 10 -X POST -H 'Content-Type: application/json' \
                --data '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}' \
                "${WEB3_RPC_URL_BACKUP}" 2>/dev/null | grep -q '"result"'; then
                pass "The backup RPC responds."
            else
                warn "The backup RPC did not answer."
            fi
        fi
    fi
fi

# ── 8. Host readiness ───────────────────────────────────────────────────────
section "Host"

command -v podman >/dev/null 2>&1 \
    && pass "podman $(podman --version | awk '{print $3}')." \
    || block "podman is not installed."

command -v podman-compose >/dev/null 2>&1 || podman compose version >/dev/null 2>&1 \
    && pass "A compose implementation is available." \
    || block "Neither podman-compose nor 'podman compose' is available."

# Rootless podman cannot bind below 1024, and the failure appears only when
# the nginx container starts — after everything else is already up.
HTTP_PORT="${HTTP_PORT:-80}"
HTTPS_PORT="${HTTPS_PORT:-443}"
UNPRIV_START="$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo 1024)"
IS_ROOTFUL=0
[[ "$(id -u)" == "0" ]] && IS_ROOTFUL=1
podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -q false && IS_ROOTFUL=1

if (( IS_ROOTFUL )); then
    pass "Running rootful; privileged ports are available."
elif (( HTTP_PORT < UNPRIV_START || HTTPS_PORT < UNPRIV_START )); then
    block "Rootless podman cannot bind ports ${HTTP_PORT}/${HTTPS_PORT} (the host allows \
from ${UNPRIV_START}). Either set HTTP_PORT/HTTPS_PORT above ${UNPRIV_START} and redirect \
to them, or raise net.ipv4.ip_unprivileged_port_start, or run rootful."
else
    pass "Ports ${HTTP_PORT}/${HTTPS_PORT} are bindable rootless."
fi

# A network that already exists is adopted with its current options, so
# `internal: true` in the compose file can be silently inert.
if podman network inspect gateway-prod-backend >/dev/null 2>&1; then
    if [[ "$(podman network inspect gateway-prod-backend --format '{{.Internal}}' 2>/dev/null)" == "true" ]]; then
        pass "gateway-prod-backend is internal; the datastores have no route out."
    else
        block "The network gateway-prod-backend exists but is NOT internal. Podman adopts \
an existing network as-is, so the compose file's 'internal: true' is being ignored and \
Postgres, Redis and Vault can reach the internet. Remove it: podman network rm gateway-prod-backend"
    fi
else
    info "gateway-prod-backend does not exist yet; it will be created internal."
fi

# Vault with mlock disabled may be swapped to disk.
if [[ "${VAULT_DISABLE_MLOCK:-true}" == "true" ]]; then
    if command -v swapon >/dev/null 2>&1 && [[ -n "$(swapon --show --noheadings 2>/dev/null)" ]]; then
        warn "VAULT_DISABLE_MLOCK=true and this host has swap enabled: Vault's memory, \
including the hot wallet key, can be written to disk. Disable or encrypt swap, or run \
rootful with VAULT_DISABLE_MLOCK=false."
    else
        pass "Vault memory locking is off, but the host has no swap."
    fi
fi

# The API pools connections per Gunicorn worker, so demand multiplies by the
# worker count. Exceeding the database's limit does not degrade — Postgres
# answers "sorry, too many clients already" and the payment fails.
POOL_SIZE="${DB_POOL_SIZE:-10}"
MAX_OVERFLOW="${DB_MAX_OVERFLOW:-10}"
WEB_WORKERS="${WEB_CONCURRENCY:-4}"
CELERY_WORKERS="${CELERY_CONCURRENCY:-4}"

# Celery uses NullPool (one connection per session, released immediately), so
# its demand tracks concurrency rather than pool size. Beat and the occasional
# migration or admin session round it up.
API_DEMAND=$(( (POOL_SIZE + MAX_OVERFLOW) * WEB_WORKERS ))
PEAK_DEMAND=$(( API_DEMAND + CELERY_WORKERS * 2 + 5 ))

info "Peak database connections: ${API_DEMAND} from the API + workers ≈ ${PEAK_DEMAND}"

if [[ "${SKIP_NETWORK}" != "1" ]] && command -v podman >/dev/null 2>&1; then
    DB_MAX="$(podman exec gateway-postgres-prod psql -U gateway -d gateway_db -t \
        -c 'SHOW max_connections;' 2>/dev/null | tr -d ' ' || true)"
    RESERVED="$(podman exec gateway-postgres-prod psql -U gateway -d gateway_db -t \
        -c 'SHOW superuser_reserved_connections;' 2>/dev/null | tr -d ' ' || true)"
    if [[ -n "${DB_MAX}" ]]; then
        USABLE=$(( DB_MAX - ${RESERVED:-3} ))
        if (( PEAK_DEMAND > USABLE )); then
            block "The stack can demand ${PEAK_DEMAND} database connections but only \
${USABLE} are usable (max_connections=${DB_MAX}, ${RESERVED:-3} reserved). Under load \
Postgres refuses the excess and those payments fail. Lower DB_POOL_SIZE or \
WEB_CONCURRENCY, or raise max_connections."
        else
            pass "Connection demand (${PEAK_DEMAND}) fits within ${USABLE} usable."
        fi
    else
        warn "Could not read max_connections; verify it exceeds ${PEAK_DEMAND}."
    fi
else
    info "Verify Postgres max_connections exceeds ${PEAK_DEMAND} before launch."
fi

AVAILABLE_KB="$(df -Pk . | awk 'NR==2 {print $4}')"
if (( AVAILABLE_KB < 5242880 )); then
    warn "Only $((AVAILABLE_KB / 1024)) MB free here. Postgres and the append-only Redis file will grow."
else
    pass "$((AVAILABLE_KB / 1048576)) GB free on this filesystem."
fi

# ── Verdict ─────────────────────────────────────────────────────────────────
echo
echo "============================================================"
if (( BLOCKERS > 0 )); then
    echo "${RED} FAILED — ${BLOCKERS} blocker(s), ${WARNINGS} warning(s).${OFF}"
    echo "============================================================"
    echo " Nothing was started. Fix the blockers and run this again."
    exit 1
fi

if (( WARNINGS > 0 )); then
    echo "${YELLOW} PASSED with ${WARNINGS} warning(s).${OFF}"
else
    echo "${GREEN} PASSED — the configuration is fit for production.${OFF}"
fi
echo "============================================================"
echo
echo " Remaining manual steps, which no script can verify for you:"
echo "   1. The acquirer has certified this terminal for live traffic."
echo "   2. The hot wallet holds enough tokens, and enough native coin for gas."
echo "   3. Vault is initialised and unsealed  (make prod-vault-unseal)."
echo "   4. The schema is migrated             (make prod-migrate)."
echo "   5. Someone is on call for the CRITICAL log lines that mean a"
echo "      cardholder is owed a manual refund."
exit 0
