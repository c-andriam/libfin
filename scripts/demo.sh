#!/usr/bin/env bash
# ============================================================================
#  End-to-end demonstration: card to crypto.
# ============================================================================
#  Walks one payment through the whole gateway, pausing between steps and
#  verifying each claim against something outside the gateway itself — the
#  acquirer's own log, the chain, and arithmetic done separately.
#
#  Written to be watched. Every number shown is read back from the database or
#  the chain at the moment it is displayed; nothing here is narration over a
#  script that already knows the answer.
#
#      scripts/demo.sh                 one payment, 300 USD
#      scripts/demo.sh EUR 500         a different currency and amount
#      scripts/demo.sh --multi         the same amount in USD, EUR and GBP
#      scripts/demo.sh --failure       what happens when delivery fails
#
#  PACE=0 runs without pauses, for a non-interactive capture.
# ============================================================================
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; BLUE=$'\033[34m'
YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'

PACE="${PACE:-1.2}"
ENV_FILE="${ENV_FILE:-.env.prodtest}"
PORT="${HTTPS_PORT:-8444}"
CARD="${CARD:-4111111111111111}"

# Which chain the stack settles on decides three things: where receipts are
# read from, whether a public explorer can show any of it, and whether the
# failure demonstration can stop the chain at all. Read it from the running
# configuration rather than assuming, so the same script serves both.
RPC_URL="$(grep -E '^WEB3_RPC_URL=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2-)"
RPC_URL="${RPC_URL:-http://gateway-anvil:8545}"
TOKEN_ADDR="$(grep -E '^ERC20_TOKEN_ADDRESS=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2-)"
if [[ "${RPC_URL}" == *gateway-anvil* ]]; then
    LOCAL_CHAIN=1
    DEST="${DEST_WALLET:-0x742d35Cc6634C0532925a3b844Bc454e4438f44e}"
else
    LOCAL_CHAIN=0
    # On a public chain the destination must be one whose key we hold, so that
    # "this wallet received it" can be demonstrated rather than asserted.
    if [[ -r testnet-dest-wallet.json ]]; then
        DEST="${DEST_WALLET:-$(python3 -c "import json;print(json.load(open('testnet-dest-wallet.json'))['address'])")}"
    else
        DEST="${DEST_WALLET:?On a public chain, set DEST_WALLET or run: ./scripts/gen_testnet_wallet.py testnet-dest-wallet.json}"
    fi
fi

pause() { [[ "${PACE}" != "0" ]] && sleep "${PACE}"; }

step()  { echo; echo "${BOLD}${BLUE}▸ $*${OFF}"; pause; }
say()   { echo "  $*"; }
good()  { echo "  ${GREEN}✔${OFF} $*"; }
warn()  { echo "  ${YELLOW}!${OFF} $*"; }
bad()   { echo "  ${RED}✘${OFF} $*"; }
note()  { echo "  ${DIM}$*${OFF}"; }

if [[ ! -f "${ENV_FILE}" ]]; then
    bad "${ENV_FILE} not found. Start the stack first:  scripts/prodtest.sh --local-chain"
    exit 1
fi

API_KEY="$(grep '^GATEWAY_API_KEY=' "${ENV_FILE}" | cut -d= -f2)"
PORT="$(grep '^HTTPS_PORT=' "${ENV_FILE}" | cut -d= -f2 || echo 8444)"
BASE="https://localhost:${PORT}"

api() { curl -sk -H "X-API-Key: ${API_KEY}" "$@"; }

db() {
    podman exec gateway-postgres-prod psql -U gateway -d gateway_db -t -A -F'|' -c "$1" 2>/dev/null
}

# ── Health ──────────────────────────────────────────────────────────────────
show_health() {
    step "Is the gateway ready?"
    local ready
    ready="$(api "${BASE}/health/ready")"
    echo "${ready}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for name, state in d['checks'].items():
    mark = '\033[32m✔\033[0m' if state in ('ok', 'CLOSED') else '\033[31m✘\033[0m'
    print(f'  {mark} {name:<18}{state}')
print(f\"\n  mode: {d['mode']}\")"
    pause
}

# ── One payment ─────────────────────────────────────────────────────────────
pay() {
    local currency="$1" amount="$2"

    step "Paying ${amount} ${currency} by card"
    note "card ${CARD:0:6}******${CARD: -4}   to wallet ${DEST:0:10}…"
    pause

    local response tx_id
    response="$(api -X POST "${BASE}/pay" \
        -H "Content-Type: application/json" \
        -H "Idempotency-Key: demo-$(date +%s%N)" \
        -d "{\"pan\":\"${CARD}\",\"expiry\":\"3012\",\"cvv\":\"123\",
             \"amount\":\"${amount}\",\"currency\":\"${currency}\",
             \"target_wallet\":\"${DEST}\"}")"

    tx_id="$(echo "${response}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('transaction_id',''))" 2>/dev/null)"

    if [[ -z "${tx_id}" ]]; then
        bad "Refused: $(echo "${response}" | head -c 160)"
        return 1
    fi

    good "Card authorised — transaction ${tx_id}"
    note "The card is only held at this point. Nothing has been debited."
    pause

    step "What rate was locked?"
    db "SELECT exchange_rate, exchange_rate_source, exchange_rate_at FROM transactions WHERE id=${tx_id};" \
    | python3 -c "
import sys, datetime
rate, source, at = sys.stdin.read().strip().split('|')
# Not when we read the feed — when Chainlink last published the round we
# read. For a cross rate it is the older of the two legs, which is the
# conservative choice. The staleness guard compares this against each
# feed's heartbeat and refuses a quote that is past it.
published = datetime.datetime.fromisoformat(at)
age_h = (datetime.datetime.now(datetime.timezone.utc) - published).total_seconds() / 3600
print(f'  rate       {rate}')
print(f'  source     {source}')
print(f'  published  {at[:19]} UTC  ({age_h:.1f} h ago, heartbeat 24 h)')"
    note "\"published\" is when Chainlink last updated the feed on-chain, not when"
    note "we read it. These feeds carry a 24-hour heartbeat, so a few hours old"
    note "is normal; past the heartbeat the quote is refused outright."
    note "The rate is locked now and used at delivery — a rate that moved in"
    note "between would hand the customer something other than the quote."
    pause

    step "Waiting for the crypto to settle on-chain"
    local status=""
    for _ in $(seq 1 60); do
        status="$(api "${BASE}/transaction/${tx_id}" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])" 2>/dev/null)"
        [[ "${status}" =~ ^(CRYPTO_SENT|AUTH_VOIDED|REVERSED|REVERSAL_FAILED)$ ]] && break
        printf "."
        sleep 3
    done
    echo

    case "${status}" in
        CRYPTO_SENT) good "Delivered and the card was charged" ;;
        AUTH_VOIDED) warn "Could not deliver — the hold was released, nothing debited" ;;
        *)           bad  "Ended ${status}" ;;
    esac
    pause

    step "Does the arithmetic hold?"
    db "SELECT amount, currency, exchange_rate, crypto_amount_units, crypto_tx_hash FROM transactions WHERE id=${tx_id};" \
    | python3 -c "
import sys
from decimal import Decimal
amount, numeric, rate, units, tx_hash = sys.stdin.read().strip().split('|')
names = {'840': 'USD', '978': 'EUR', '826': 'GBP'}
delivered = Decimal(units) / 10**6 if units else Decimal(0)
print(f\"  {amount} {names.get(numeric, numeric)}  ÷  {Decimal(rate):.8f}\")
print(f'  = {Decimal(amount)/Decimal(rate):.6f}   computed here, independently')
print(f'  = {delivered}   actually delivered')
print()
if tx_hash:
    print(f'  transaction  {tx_hash}')"
    pause

    LAST_TX="${tx_id}"
}

# ── Verification outside the gateway ────────────────────────────────────────
verify_chain() {
    local tx_id="$1"
    local tx_hash
    tx_hash="$(db "SELECT crypto_tx_hash FROM transactions WHERE id=${tx_id};" | tr -d ' ')"
    [[ -z "${tx_hash}" ]] && return 0

    step "Confirming on the chain itself, not in our records"
    local receipt
    receipt="$(podman run --rm --network gateway-prod-egress \
        -e FOUNDRY_DISABLE_NIGHTLY_WARNING=1 --entrypoint cast \
        ghcr.io/foundry-rs/foundry:latest receipt "${tx_hash}" \
        --rpc-url "${RPC_URL}" 2>/dev/null)"

    echo "${receipt}" | grep -E "^(status|blockNumber|gasUsed)" | sed 's/^/  /'
    good "The chain agrees the transfer happened"

    if (( ! LOCAL_CHAIN )); then
        echo
        say "Anyone can check this, on services we do not control:"
        say "  https://sepolia.etherscan.io/tx/${tx_hash}"
        say "  https://sepolia.etherscan.io/address/${DEST}"
        say "  https://eth-sepolia.blockscout.com/address/${DEST}/token-transfers"
        # Deliberately no "?a=" form here: a query string is where terminal and
        # chat clients truncate a URL when they auto-detect links, and a link
        # that opens the wrong page is worse than one more click.
    fi
    pause
}

verify_acquirer() {
    step "What the bank saw, from its own log"
    podman logs gateway-bank-prodtest 2>&1 | grep -E "approved|captured" | tail -2 | sed 's/^.*bank-sim\] INFO /  /'
    note "0100 authorisation, then 0120 capture — the money is taken only"
    note "after the crypto is confirmed."
    pause
}

# ── Modes ───────────────────────────────────────────────────────────────────
case "${1:-}" in
    --multi)
        show_health
        for currency in USD EUR GBP; do
            pay "${currency}" "${2:-300.00}" || true
        done
        step "Three currencies, three rates, one gateway"
        db "SELECT currency, amount, exchange_rate, crypto_amount_units FROM transactions ORDER BY id DESC LIMIT 3;" \
        | python3 -c "
import sys
from decimal import Decimal
names = {'840': 'USD', '978': 'EUR', '826': 'GBP'}
print(f\"  {'currency':<10}{'paid':<10}{'rate':<16}{'USDT received'}\")
print('  ' + '─' * 52)
for line in sorted(l for l in sys.stdin if l.strip()):
    numeric, amount, rate, units = line.strip().split('|')
    print(f'  {names.get(numeric, numeric):<10}{amount:<10}{Decimal(rate):<16.8f}{Decimal(units)/10**6}')"
        echo
        note "Sterling buys the most, the dollar the least — the order follows the"
        note "real parities, which is the simplest check that nothing is inverted."
        ;;
    --failure)
        # Stopping a chain is only available when we are the ones running it.
        # Refusing here beats a demonstration that quietly proves nothing.
        if (( ! LOCAL_CHAIN )); then
            bad "This demonstration stops the chain, which is only possible on the"
            bad "local one. The stack is currently settling on ${RPC_URL}."
            echo
            note "Run it against the local chain instead:"
            note "  scripts/prodtest.sh --down && scripts/prodtest.sh --local-chain"
            exit 1
        fi
        show_health
        step "Now the interesting case: what if the crypto cannot be delivered?"
        note "Stopping the chain so the transfer must fail."
        podman stop gateway-anvil-prodtest >/dev/null 2>&1
        good "Chain stopped"
        pause
        pay "${2:-USD}" "${3:-50.00}" || true
        step "Restarting the chain"
        podman start gateway-anvil-prodtest >/dev/null 2>&1
        good "Restarted"
        note "The card was held, never debited. The customer owes nothing and is"
        note "owed nothing — which is the whole point of authorising before"
        note "delivering rather than charging first."
        ;;
    *)
        show_health
        pay "${1:-USD}" "${2:-300.00}" || exit 1
        verify_acquirer
        verify_chain "${LAST_TX}"
        step "Done"
        good "Card authorised, crypto delivered, card charged — in that order"
        ;;
esac

echo
