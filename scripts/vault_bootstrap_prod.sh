#!/usr/bin/env bash
# First start of production Vault: initialise, unseal, write the token.
#
# There is an ordering problem without this. `make prod` runs preflight, which
# blocks while VAULT_TOKEN is still a placeholder — but the token does not
# exist until Vault has been initialised, and Vault is part of the stack that
# preflight is guarding. scripts/prodtest.sh solves this internally for the
# rehearsal; production had no documented way through.
#
# So this brings up Vault *alone*, deliberately bypassing preflight. Nothing
# else starts: no API, no worker, no acquirer link, nothing that could touch a
# card. Vault on its own is inert.
#
# Run once, at the very beginning:
#     make prod-vault-bootstrap        (or: scripts/vault_bootstrap_prod.sh)
#
# Then fill in the remaining values and run `make prod-full` as usual.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO}/.env.prod.local}"
CONTAINER="gateway-vault-prod"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
say()  { echo "  $*"; }
good() { echo "  ${GREEN}✔${OFF} $*"; }
bad()  { echo "  ${RED}✘${OFF} $*"; }

[[ -f "${ENV_FILE}" ]] || { bad "${ENV_FILE} not found. Run: make env-prod"; exit 1; }

COMPOSE=(podman-compose -f "${REPO}/podman-compose.prod.yml" --env-file "${ENV_FILE}")
VAULT_EXEC=(podman exec -e VAULT_ADDR=http://127.0.0.1:8200 "${CONTAINER}")

echo
echo "${BOLD}Starting Vault alone${OFF} — no application container is started here."
"${COMPOSE[@]}" up -d gateway-vault >/dev/null 2>&1

for _ in {1..20}; do
    [[ "$(podman inspect "${CONTAINER}" --format '{{.State.Status}}' 2>/dev/null)" == "running" ]] && break
    sleep 2
done
[[ "$(podman inspect "${CONTAINER}" --format '{{.State.Status}}' 2>/dev/null)" == "running" ]] \
    || { bad "Vault did not start. Check: podman logs ${CONTAINER}"; exit 1; }

# Never re-initialise. A second `operator init` against a Vault that already
# holds the hot wallet key would be refused by Vault itself, but checking first
# turns a confusing error into a clear one — and makes this safe to re-run.
# Captured, not piped: under `set -o pipefail` a sealed Vault's exit code 2
# would become the pipeline's, and the test would fail whatever grep found.
if grep -q "Initialized.*true" \
        <<<"$("${VAULT_EXEC[@]}" vault status 2>/dev/null || true)"; then
    echo
    good "Vault is already initialised — nothing to do."
    say  "If it is sealed:      make prod-vault-unseal"
    say  "If you lost the token, it cannot be recovered: the unseal keys from"
    say  "the original initialisation are the only way back in."
    exit 0
fi

echo
say "Initialising with 5 key shares, 3 required to unseal."
INIT_JSON="$("${VAULT_EXEC[@]}" vault operator init -key-shares=5 -key-threshold=3 -format=json 2>/dev/null)"
[[ -n "${INIT_JSON}" ]] || { bad "Vault would not initialise. Check: podman logs ${CONTAINER}"; exit 1; }

ROOT_TOKEN="$(echo "${INIT_JSON}" | python3 -c "import json,sys;print(json.load(sys.stdin)['root_token'])")"
mapfile -t UNSEAL_KEYS < <(echo "${INIT_JSON}" | python3 -c \
    "import json,sys;[print(k) for k in json.load(sys.stdin)['unseal_keys_hex']]")

# Unseal with the first three. The remaining two exist so that losing one
# custodian does not lock you out permanently.
for i in 0 1 2; do
    "${VAULT_EXEC[@]}" vault operator unseal "${UNSEAL_KEYS[$i]}" >/dev/null 2>&1
done
# Captured, not piped: under `set -o pipefail` a sealed Vault's exit code 2
# would become the pipeline's, and the test would fail whatever grep found.
grep -q "Sealed.*false" \
        <<<"$("${VAULT_EXEC[@]}" vault status 2>/dev/null || true)" \
    || { bad "Vault is still sealed after three keys."; exit 1; }

# Only the token goes into the file. The unseal keys are deliberately NOT
# written anywhere: a production Vault whose unseal keys sit next to it on the
# same disk is a Vault that protects nothing. They are printed once, here, and
# it is on you to put them somewhere else.
sed -i "s|^VAULT_TOKEN=.*|VAULT_TOKEN=${ROOT_TOKEN}|" "${ENV_FILE}"

echo
echo "${BOLD}════════════════════════════════════════════════════════════${OFF}"
echo "${YELLOW}${BOLD} Written down now, or lost forever.${OFF}"
echo "${BOLD}════════════════════════════════════════════════════════════${OFF}"
echo
echo "  ${BOLD}Unseal keys${OFF} — any 3 of these 5 unseal Vault after a restart."
echo "  They are NOT saved to disk. Split them between custodians, offline."
echo
for i in "${!UNSEAL_KEYS[@]}"; do
    printf '    %d.  %s\n' "$((i + 1))" "${UNSEAL_KEYS[$i]}"
done
echo
echo "  ${BOLD}Root token${OFF} — written into ${ENV_FILE} for you."
printf '        %s\n' "${ROOT_TOKEN}"
echo
echo "${BOLD}════════════════════════════════════════════════════════════${OFF}"
echo
good "Vault initialised, unsealed, and VAULT_TOKEN filled in."
say  "Vault comes back ${BOLD}sealed${OFF} after every restart of its container."
say  "Whenever that happens:  make prod-vault-unseal  (three times)"
echo
say  "Next:  store the hot wallet key   make prod-vault-secrets"
say  "       then continue filling in   ${ENV_FILE}"
echo
