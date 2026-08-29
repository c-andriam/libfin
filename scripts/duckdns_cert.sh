#!/usr/bin/env bash
#
#  Issue a real Let's Encrypt certificate for a DuckDNS name, without needing
#  a single inbound connection.
#
#  Why DNS-01 and not the usual HTTP-01: HTTP-01 asks Let's Encrypt to fetch a
#  file from port 80 of the machine the name points at. Behind NAT with no port
#  forwarding — the common case on a home or mobile connection — nothing can
#  reach that port, so the challenge can never succeed. DNS-01 instead asks for
#  a TXT record, which DuckDNS lets us set over its API from the inside. The
#  certificate that comes out is identical: publicly trusted, browser-valid.
#
#  Nothing here needs root. Certbot runs from the project's own venv and keeps
#  its state under .certbot/, not /etc.
#
#      DUCKDNS_TOKEN=... scripts/duckdns_cert.sh sapy.duckdns.org you@example.com
#
set -euo pipefail
cd "$(dirname "$0")/.."

BLUE='\033[1;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'
say() { echo -e "${BLUE}▸${NC} $*"; }
ok()  { echo -e "  ${GREEN}✔${NC} $*"; }
bad() { echo -e "  ${RED}✘${NC} $*"; }

DOMAIN="${1:?usage: DUCKDNS_TOKEN=... $0 <name>.duckdns.org [email]}"
EMAIL="${2:-}"
: "${DUCKDNS_TOKEN:?Set DUCKDNS_TOKEN. Never pass it as an argument: command
   lines are visible to every process on the machine through ps.}"

case "${DOMAIN}" in
    *.duckdns.org) SUB="${DOMAIN%.duckdns.org}" ;;
    *) bad "Expected a .duckdns.org name, got ${DOMAIN}"; exit 1 ;;
esac

CERTBOT=".certbot-venv/bin/certbot"
[[ -x "${CERTBOT}" ]] || { bad "certbot venv missing. Run: python3 -m venv .certbot-venv && .certbot-venv/bin/pip install certbot"; exit 1; }

# The hooks run as separate processes, so the token reaches them through the
# environment, which certbot passes down. Written to a file certbot can exec.
HOOK_DIR=".certbot/hooks"
mkdir -p "${HOOK_DIR}"

cat > "${HOOK_DIR}/auth.sh" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
sub="${DUCKDNS_SUB}"
resp="$(curl -s "https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&txt=${CERTBOT_VALIDATION}")"
[[ "${resp}" == "OK" ]] || { echo "DuckDNS refused the TXT update: ${resp}" >&2; exit 1; }
# DuckDNS applies the record immediately, but Let's Encrypt queries the
# authoritative servers, and a validator that asks too early sees the old
# answer and fails the whole order. Waiting is cheaper than a failed order.
sleep 30
HOOK

cat > "${HOOK_DIR}/cleanup.sh" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
# Leaving a stale validation TXT behind is harmless but untidy, and a leftover
# record confuses the next issuance if it ever runs concurrently.
curl -s "https://www.duckdns.org/update?domains=${DUCKDNS_SUB}&token=${DUCKDNS_TOKEN}&txt=removed&clear=true" >/dev/null || true
HOOK
chmod +x "${HOOK_DIR}"/*.sh

say "Checking the token can write to ${DOMAIN}"
PROBE="$(curl -s "https://www.duckdns.org/update?domains=${SUB}&token=${DUCKDNS_TOKEN}&txt=probe")"
if [[ "${PROBE}" != "OK" ]]; then
    bad "DuckDNS rejected the token (answered: ${PROBE:-<empty>})."
    echo "  Check the token on duckdns.org, and that ${SUB} is on that account."
    exit 1
fi
curl -s "https://www.duckdns.org/update?domains=${SUB}&token=${DUCKDNS_TOKEN}&txt=removed&clear=true" >/dev/null
ok "Token accepted"

say "Asking Let's Encrypt for a certificate (DNS-01, no inbound port needed)"
EMAIL_ARGS=(--register-unsafely-without-email)
[[ -n "${EMAIL}" ]] && EMAIL_ARGS=(--email "${EMAIL}" --no-eff-email)

DUCKDNS_SUB="${SUB}" DUCKDNS_TOKEN="${DUCKDNS_TOKEN}" \
"${CERTBOT}" certonly \
    --manual --preferred-challenges dns \
    --manual-auth-hook "$(pwd)/${HOOK_DIR}/auth.sh" \
    --manual-cleanup-hook "$(pwd)/${HOOK_DIR}/cleanup.sh" \
    --config-dir .certbot/config --work-dir .certbot/work --logs-dir .certbot/logs \
    --agree-tos "${EMAIL_ARGS[@]}" \
    --non-interactive --keep-until-expiring \
    -d "${DOMAIN}"

LIVE=".certbot/config/live/${DOMAIN}"
[[ -r "${LIVE}/fullchain.pem" ]] || { bad "Certbot finished but produced no certificate."; exit 1; }

say "Installing it where Nginx reads its certificate"
install -m 644 "${LIVE}/fullchain.pem" nginx/ssl/server.crt
install -m 600 "${LIVE}/privkey.pem"   nginx/ssl/server.key
ok "nginx/ssl/server.crt and server.key replaced"

echo
openssl x509 -in nginx/ssl/server.crt -noout -subject -issuer -dates | sed 's/^/  /'
echo
echo -e "  ${DIM}Renew with the same command; --keep-until-expiring makes it a no-op${NC}"
echo -e "  ${DIM}until the certificate is within 30 days of expiry.${NC}"
