#!/bin/sh
# Deploy SimToken onto the local Anvil chain.
#
# Runs inside the Foundry image (which carries solc), waits for the chain, then
# deploys from Anvil's first account. Because that account deploys nothing else
# and starts at nonce 0, the contract address is deterministic —
# 0x5FbDB2315678afecb367f032d93F642f64180aa3 — which is what .env.sim points
# ERC20_TOKEN_ADDRESS at. No address file to plumb around.
set -eu

RPC_URL="${RPC_URL:-http://gateway-anvil:8545}"
# Anvil's well-known first account. A published test key: never reuse it.
DEPLOYER_KEY="${DEPLOYER_KEY:-0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80}"
EXPECTED_ADDRESS="0x5FbDB2315678afecb367f032d93F642f64180aa3"
# One billion tokens at six decimals.
INITIAL_SUPPLY="${INITIAL_SUPPLY:-1000000000000000}"

echo "Waiting for the chain at ${RPC_URL}..."
i=0
until cast block-number --rpc-url "${RPC_URL}" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "${i}" -gt 60 ]; then
        echo "ERROR: no chain at ${RPC_URL} after 60s." >&2
        exit 1
    fi
    sleep 1
done

# Idempotent: re-running `make sim` against a live chain must not fail.
CODE=$(cast code "${EXPECTED_ADDRESS}" --rpc-url "${RPC_URL}" 2>/dev/null || echo "0x")
if [ "${CODE}" != "0x" ] && [ -n "${CODE}" ]; then
    echo "SimToken is already deployed at ${EXPECTED_ADDRESS}."
    exit 0
fi

echo "Deploying SimToken..."
# Build in a scratch directory: /sim is a read-only-ish bind mount owned by the
# host user, and forge wants to write its artefacts next to the source.
WORKDIR="${WORKDIR:-/tmp/simtoken}"
rm -rf "${WORKDIR}"
mkdir -p "${WORKDIR}/src"
cp /sim/SimToken.sol "${WORKDIR}/src/SimToken.sol"
cd "${WORKDIR}"

cat > foundry.toml <<'TOML'
[profile.default]
src = "src"
out = "out"
libs = []
TOML

forge create src/SimToken.sol:SimToken \
    --rpc-url "${RPC_URL}" \
    --private-key "${DEPLOYER_KEY}" \
    --broadcast \
    --constructor-args "${INITIAL_SUPPLY}"

DEPLOYED_CODE=$(cast code "${EXPECTED_ADDRESS}" --rpc-url "${RPC_URL}" 2>/dev/null || echo "0x")
if [ "${DEPLOYED_CODE}" = "0x" ] || [ -z "${DEPLOYED_CODE}" ]; then
    echo "ERROR: nothing deployed at the expected address ${EXPECTED_ADDRESS}." >&2
    echo "The deployer account was not at nonce 0. Reset the chain with 'make sim-down'." >&2
    exit 1
fi

echo "SimToken deployed at ${EXPECTED_ADDRESS}."

# Fund the gateway's hot wallet so transfers have something to move.
if [ -n "${GATEWAY_WALLET:-}" ]; then
    echo "Funding the gateway hot wallet ${GATEWAY_WALLET}..."
    cast send "${EXPECTED_ADDRESS}" "mint(address,uint256)" \
        "${GATEWAY_WALLET}" "${INITIAL_SUPPLY}" \
        --rpc-url "${RPC_URL}" --private-key "${DEPLOYER_KEY}" >/dev/null
    # Gas money for the transfers themselves.
    cast send "${GATEWAY_WALLET}" --value 10ether \
        --rpc-url "${RPC_URL}" --private-key "${DEPLOYER_KEY}" >/dev/null
    echo "Hot wallet funded with tokens and gas."
fi
