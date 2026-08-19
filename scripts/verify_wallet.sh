#!/usr/bin/env bash
#
#  Does the wallet really hold what we say we delivered?
#
#  This asks the chain, not the gateway. The gateway's own records are what is
#  being checked, so using them as the source of truth would prove nothing: it
#  reads the token contract's balanceOf directly, sums what our database claims
#  was delivered to that address, and puts the two numbers side by side.
#
#  A public block explorer cannot answer this for a rehearsal, because the local
#  Anvil chain is reachable from this machine and nowhere else. On a public
#  testnet the same figures are visible to anyone at:
#      https://sepolia.etherscan.io/token/<token>?a=<wallet>
#
#      scripts/verify_wallet.sh [wallet-address]
#
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env.prodtest"
BOLD='\033[1m'; BLUE='\033[1;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'
DIM='\033[2m'; NC='\033[0m'

[[ -r "${ENV_FILE}" ]] || { echo -e "${RED}No ${ENV_FILE}. Start the stack: make demo-up${NC}"; exit 1; }

TOKEN="$(grep -E '^ERC20_TOKEN_ADDRESS=' "${ENV_FILE}" | cut -d= -f2-)"
RPC="$(grep -E '^WEB3_RPC_URL=' "${ENV_FILE}" | cut -d= -f2-)"
RPC="${RPC:-http://gateway-anvil:8545}"
LOCAL_CHAIN=0; [[ "${RPC}" == *gateway-anvil* ]] && LOCAL_CHAIN=1

# The default destination differs by chain for the same reason the demo's does:
# locally it is a throwaway address, publicly it must be one whose key we hold.
if [[ -n "${1:-}" ]]; then
    WALLET="$1"
elif (( LOCAL_CHAIN )); then
    WALLET="0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
elif [[ -r testnet-dest-wallet.json ]]; then
    WALLET="$(python3 -c "import json;print(json.load(open('testnet-dest-wallet.json'))['address'])")"
else
    echo -e "${RED}Pass a wallet address: scripts/verify_wallet.sh 0x…${NC}"; exit 1
fi

# Run cast from a throwaway container on the egress network rather than inside
# the chain container, so the same call reaches a local Anvil and a public RPC.
cast_run() { podman run --rm --network gateway-prod-egress \
                 -e FOUNDRY_DISABLE_NIGHTLY_WARNING=1 --entrypoint cast \
                 ghcr.io/foundry-rs/foundry:latest "$@" 2>/dev/null; }

chain() { cast_run call "${TOKEN}" "$@" --rpc-url "${RPC}" | tail -1; }

db() { podman exec gateway-postgres-prod psql -U gateway -d gateway_db -tAc "$1" 2>/dev/null; }

echo
echo
if (( LOCAL_CHAIN )); then
    echo -e "${DIM}  Settling on the local chain — no public explorer can see any of it.${NC}"
else
    echo -e "${BLUE}▸ The same figures, on services we do not control${NC}"
    # None of these carry a query string, because clients that auto-detect
    # links routinely cut a URL at the "?" and land the reader somewhere else.
    echo -e "  https://eth-sepolia.blockscout.com/address/${WALLET}/token-transfers"
    echo -e "  https://sepolia.etherscan.io/address/${WALLET}"
    echo -e "  https://sepolia.etherscan.io/token/${TOKEN}"
fi

echo
echo -e "${BLUE}▸ Asking the token contract directly${NC}"
SYMBOL="$(chain 'symbol()(string)' | tr -d '"')"
DECIMALS="$(chain 'decimals()(uint8)')"
RAW="$(chain 'balanceOf(address)(uint256)' "${WALLET}" | awk '{print $1}')"

echo -e "  token     ${TOKEN}  (${SYMBOL}, ${DECIMALS} decimals)"
echo -e "  wallet    ${WALLET}"
echo -e "  balance   ${BOLD}$(python3 -c "
from decimal import Decimal
print(f'{Decimal('${RAW}') / (10 ** ${DECIMALS}):.{${DECIMALS}}f}')") ${SYMBOL}${NC}"
echo -e "  ${DIM}Read from the chain's own state, with no help from the gateway.${NC}"

echo
echo -e "${BLUE}▸ What our records claim we sent there${NC}"
# Summed in the token's own minor units, the same integers the chain holds,
# so the comparison below is exact rather than a decimal round-trip.
CLAIMED="$(db "SELECT COALESCE(SUM(crypto_amount_units),0) FROM transactions
               WHERE status='CRYPTO_SENT' AND lower(target_wallet)=lower('${WALLET}');")"
COUNT="$(db "SELECT COUNT(*) FROM transactions
             WHERE status='CRYPTO_SENT' AND lower(target_wallet)=lower('${WALLET}');")"
echo -e "  ${COUNT} settled transaction(s) totalling ${BOLD}$(python3 -c "
from decimal import Decimal
print(f'{Decimal('${CLAIMED}') / (10 ** ${DECIMALS}):.{${DECIMALS}}f}')") ${SYMBOL}${NC}"
echo -e "  ${DIM}${CLAIMED} minor units, as recorded at the moment of transfer.${NC}"

echo
echo -e "${BLUE}▸ Do they agree?${NC}"
python3 - "${RAW}" "${DECIMALS}" "${CLAIMED}" <<'PY'
import sys
from decimal import Decimal
# Compared as integers in minor units. Converting to a decimal first would
# introduce the very rounding this check exists to rule out.
raw, decimals, claimed = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3] or 0)
on_chain = Decimal(raw) / (10 ** decimals)
print(f"  on chain  {on_chain:.{decimals}f}   ({raw} units)")
print(f"  claimed   {Decimal(claimed) / (10 ** decimals):.{decimals}f}   ({claimed} units)")
diff = Decimal(raw - claimed) / (10 ** decimals)
if diff == 0:
    print("\n  \033[0;32m✔\033[0m The wallet holds exactly what we recorded delivering.")
else:
    print(f"\n  \033[0;31m✘\033[0m Discrepancy of {diff:+.{decimals}f}.")
    print("    A positive figure means the wallet was funded from somewhere")
    print("    other than this gateway — on a shared address that is expected.")
    print("    A negative figure is the one that matters: we recorded a delivery")
    print("    the chain does not show.")
    sys.exit(1)
PY

echo
echo -e "${BLUE}▸ Each transfer, confirmed against its own receipt${NC}"
db "SELECT id, crypto_amount_units, crypto_tx_hash FROM transactions
    WHERE status='CRYPTO_SENT' AND lower(target_wallet)=lower('${WALLET}')
    ORDER BY id;" | while IFS='|' read -r id amount hash; do
    [[ -z "${hash}" ]] && continue
    st="$(cast_run receipt "${hash}" status --rpc-url "${RPC}" | tail -1)"
    # cast prints "true"/"false" for a receipt's status field; older builds and
    # raw JSON-RPC print 1/0. Accept both rather than depend on the build.
    if [[ "${st}" == "1" || "${st}" == "true" ]]; then
        echo -e "  ${GREEN}✔${NC} tx ${id}  ${amount} units  ${hash:0:18}…  receipt status 1"
    else
        echo -e "  ${RED}✘${NC} tx ${id}  ${amount} units  ${hash:0:18}…  receipt status ${st:-absent}"
    fi
done
echo
