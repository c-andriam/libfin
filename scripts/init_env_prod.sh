#!/usr/bin/env bash
# Create .env.prod from the example, generating the secrets that can be
# generated and listing the ones that cannot.
#
# Four values in the example are just random bytes: two datastore passwords,
# the gateway API key and the PAN encryption key. Nobody should be inventing
# those by hand, and nobody should be copying them between machines either —
# so they are generated here, on the host that will use them, and the file is
# left at 0600.
#
# Everything else marked REPLACE_ME comes from outside: the acquirer's
# implementation guide, an RPC provider, and Vault's own initialisation. The
# script names each one rather than guessing, and `make prod-preflight` refuses
# to launch while any remains.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="${REPO}/.env.prod.example"
# .env.prod is tracked, and this repository is public: it is the template.
# The real file is .env.prod.local, which .gitignore keeps out of git.
TARGET="${ENV_FILE:-${REPO}/.env.prod.local}"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { echo "  $*"; }
good() { echo "  ${GREEN}✔${OFF} $*"; }
warn() { echo "  ${YELLOW}!${OFF} $*"; }

[[ -f "${EXAMPLE}" ]] || { echo "Missing ${EXAMPLE}"; exit 1; }

# Refusing to overwrite matters more here than anywhere else in this repo: the
# file holds the only copy of the PAN encryption key, and losing it means the
# card numbers staged for a reversal can never be decrypted — every cardholder
# owed a refund would have to be refunded by hand.
if [[ -e "${TARGET}" && "${FORCE:-}" != "1" ]]; then
    echo
    warn "${TARGET} already exists — not touching it."
    say  "Its PAN encryption key may be the only copy: overwriting it would"
    say  "strand any card number staged for a reversal."
    say  "${DIM}Re-run with FORCE=1 only if you are certain.${OFF}"
    exit 0
fi

command -v openssl >/dev/null || { echo "openssl is required"; exit 1; }

# hex, not base64, for the two that get embedded in a URL: base64's '/', '+'
# and '=' are not safe in a URL's userinfo field, and DATABASE_URL would parse
# wrong — or worse, parse differently than you expect. 32 bytes either way.
POSTGRES_PW="$(openssl rand -hex 32)"
REDIS_PW="$(openssl rand -hex 32)"
API_KEY="$(openssl rand -hex 32)"          # preflight wants 32 characters min
PAN_KEY="$(openssl rand -base64 32)"       # never appears in a URL

# Published ports. The example ships 80/443, which assumes a rootful host —
# under rootless podman the stack builds, migrates, starts every datastore, and
# only then fails when nginx cannot bind. Preflight catches it first, but only
# to send the operator back here to edit by hand. Deciding it at the moment the
# file is written saves that round trip.
#
# The detection mirrors scripts/preflight_check.sh so the two cannot disagree.
UNPRIV_START="$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo 1024)"
IS_ROOTFUL=0
[[ "$(id -u)" == "0" ]] && IS_ROOTFUL=1
podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -q false && IS_ROOTFUL=1

if (( IS_ROOTFUL )); then
    HTTP_PORT=80
    HTTPS_PORT=443
    PORT_NOTE="rootful host, standard ports"
else
    # Clear of the other stacks on the same machine: sim takes 8080/8443, and
    # scripts/prodtest.sh takes 8081/8444.
    HTTP_PORT=9080
    HTTPS_PORT=9443
    PORT_NOTE="rootless podman cannot bind below ${UNPRIV_START}"
fi

umask 077
cp "${EXAMPLE}" "${TARGET}"

# '|' as the delimiter: hex and base64 contain '/', and base64 contains '+',
# but neither alphabet contains '|', '&' or a backslash, so no escaping games.
sed -i \
    -e "s|REPLACE_ME_POSTGRES_PASSWORD|${POSTGRES_PW}|g" \
    -e "s|REPLACE_ME_REDIS_PASSWORD|${REDIS_PW}|g" \
    -e "s|REPLACE_ME_API_KEY|${API_KEY}|g" \
    -e "s|REPLACE_ME_PAN_ENCRYPTION_KEY|${PAN_KEY}|g" \
    "${TARGET}"
sed -i -e "s|^HTTP_PORT=.*|HTTP_PORT=${HTTP_PORT}|" -e "s|^HTTPS_PORT=.*|HTTPS_PORT=${HTTPS_PORT}|" "${TARGET}"
chmod 600 "${TARGET}"

echo
echo "${BOLD}Written ${TARGET}${OFF} (mode 600)"
echo
good "POSTGRES_PASSWORD, REDIS_PASSWORD   generated, and substituted into"
say  "                                    DATABASE_URL, REDIS_URL and"
say  "                                    CELERY_BROKER_URL"
good "GATEWAY_API_KEY                     generated — Nginx now injects this"
say  "                                    into /pay and /transaction, so no"
say  "                                    browser ever holds it"
good "PAN_ENCRYPTION_KEY                  generated — back this up, or a"
say  "                                    reversal cannot decrypt its PAN"
good "HTTP_PORT=${HTTP_PORT}, HTTPS_PORT=${HTTPS_PORT}          ${PORT_NOTE}"

# What is left is what no script can know.
echo
echo "${BOLD}Still to fill in${OFF} — preflight blocks until every one is gone:"
echo
# Assignments only: the header comments mention REPLACE_ME too, and listing
# them as work to do is noise.
REMAINING="$(grep -n '^[A-Z0-9_]*=.*REPLACE_ME' "${TARGET}" || true)"
if [[ -z "${REMAINING}" ]]; then
    good "none"
else
    echo "${REMAINING}" | while IFS= read -r line; do
        printf '  %s%s%s\n' "${DIM}" "${line}" "${OFF}"
    done
    echo
    say "${BOLD}From your acquirer's implementation guide${OFF}"
    say "  BANK_HOST, BANK_PORT, ACQUIRER_TERMINAL_ID, ACQUIRER_MERCHANT_ID,"
    say "  ACQUIRER_INSTITUTION_ID, ACQUIRER_NAME, ACQUIRER_CITY"
    say "  ${DIM}Also confirm with them: ACQUIRER_CAPTURE_MODE, ACQUIRER_HEX_BITMAP,${OFF}"
    say "  ${DIM}ACQUIRER_ENCODING and the merchant category code. Do not guess —${OFF}"
    say "  ${DIM}the wrong dialect means every message is rejected as unparseable.${OFF}"
    echo
    say "${BOLD}From your RPC provider${OFF}"
    say "  WEB3_RPC_URL, WEB3_RPC_URL_BACKUP"
    echo
    say "${BOLD}From Vault, after the stack is up${OFF}"
    say "  VAULT_TOKEN — 'make prod-vault-init' prints it, together with the"
    say "  unseal keys. Keep both offline; without them the secrets are gone."
fi

# Two values preflight accepts but nobody should accept blindly.
echo
echo "${BOLD}Worth a decision before launch${OFF}"
say "  CORS_ORIGINS   Nginx now serves the form from the same origin as the"
say "                 API, so nothing needs CORS. Preflight only refuses '*';"
say "                 leaving a stale domain here is harmless but misleading."
say "  TRUSTED_PROXIES  currently '*'. Preflight does not check this. Left as"
say "                 is, anyone who can reach the API port forges"
say "                 X-Forwarded-For and walks past the rate limit. It is"
say "                 only defensible because the API port is never published."
echo
say "Form and API will both be on https://localhost:${HTTPS_PORT}/"
say "Next:  make certs   then   make prod-full"
echo
