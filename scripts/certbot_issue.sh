#!/usr/bin/env bash
# Obtain a real TLS certificate from Let's Encrypt, and install it for Nginx.
#
# Until this runs, nginx/ssl holds the self-signed certificate `make certs`
# generates. That is fine for simulation and rehearsal, and useless in front of
# a cardholder: every browser refuses it, so the form never loads at all.
#
# How it works. nginx already serves /.well-known/acme-challenge/ from
# /var/www/certbot on port 80 — the one path the HTTP server does not redirect
# to HTTPS. This writes the challenge there through a shared volume, so the
# certificate is issued without stopping the gateway.
#
# Usage:
#     make prod-certbot DOMAIN=pay.example.com EMAIL=ops@example.com
#     STAGING=1 make prod-certbot DOMAIN=... EMAIL=...    # dry run, no rate limit
#
# Let's Encrypt allows 5 failed attempts per account per hour. Rehearse with
# STAGING=1 first: a staging certificate is untrusted but proves the whole
# path works, and staging has far looser limits.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO}/.env.prod.local}"
SSL_DIR="${REPO}/nginx/ssl"
WEBROOT="${REPO}/nginx/certbot-webroot"
LETSENCRYPT="${REPO}/nginx/letsencrypt"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
say()  { echo "  $*"; }
good() { echo "  ${GREEN}✔${OFF} $*"; }
warn() { echo "  ${YELLOW}!${OFF} $*"; }
bad()  { echo "  ${RED}✘${OFF} $*"; }

[[ -n "${DOMAIN:-}" ]] || { bad "Usage: make prod-certbot DOMAIN=pay.example.com EMAIL=ops@example.com"; exit 1; }
[[ -n "${EMAIL:-}"  ]] || { bad "EMAIL is required: Let's Encrypt sends expiry warnings there."; exit 1; }

HTTP_PORT="$(sed -n 's/^HTTP_PORT=//p' "${ENV_FILE}" 2>/dev/null | tr -d '\r' | tail -1)"
HTTP_PORT="${HTTP_PORT:-80}"

# The ACME HTTP-01 challenge is fetched on port 80 of the domain, by Let's
# Encrypt itself. Nothing we can configure changes that: if the stack publishes
# 8080 because it runs rootless, something in front must forward 80 to it.
if [[ "${HTTP_PORT}" != "80" ]]; then
    echo
    warn "HTTP_PORT=${HTTP_PORT}, but Let's Encrypt always fetches the challenge on port 80."
    say  "Forward it on the host before continuing, for example:"
    say  "  sudo nft add rule inet nat prerouting tcp dport 80 redirect to :${HTTP_PORT}"
    say  "or run the stack rootful with HTTP_PORT=80."
    echo
    read -r -p "  Port 80 already reaches this stack? [y/N] " reply
    [[ "${reply}" == "y" ]] || exit 1
fi

mkdir -p "${WEBROOT}" "${LETSENCRYPT}" "${SSL_DIR}"

STAGING_ARG=()
if [[ "${STAGING:-}" == "1" ]]; then
    STAGING_ARG=(--staging)
    warn "Staging mode: the certificate will NOT be trusted by browsers."
fi

echo
echo "${BOLD}Requesting a certificate for ${DOMAIN}${OFF}"
podman run --rm \
    -v "${LETSENCRYPT}:/etc/letsencrypt:z" \
    -v "${WEBROOT}:/var/www/certbot:z" \
    docker.io/certbot/certbot:latest \
    certonly --webroot -w /var/www/certbot \
    -d "${DOMAIN}" \
    --email "${EMAIL}" \
    --agree-tos --no-eff-email --non-interactive \
    "${STAGING_ARG[@]}"

LIVE="${LETSENCRYPT}/live/${DOMAIN}"
[[ -f "${LIVE}/fullchain.pem" ]] || { bad "No certificate at ${LIVE}. Nothing was installed."; exit 1; }

# nginx.conf reads server.crt / server.key, so the issued pair is copied to
# those names rather than the config being rewritten: one less thing that can
# diverge between the simulation, the rehearsal and production.
#
# fullchain, not cert: without the intermediate, many clients — mobile ones
# above all — reject the chain even though the leaf is perfectly valid.
cp "${LIVE}/fullchain.pem" "${SSL_DIR}/server.crt"
cp "${LIVE}/privkey.pem"   "${SSL_DIR}/server.key"
chmod 644 "${SSL_DIR}/server.crt"
chmod 600 "${SSL_DIR}/server.key"

echo
good "Installed into ${SSL_DIR}/server.crt and server.key"
openssl x509 -in "${SSL_DIR}/server.crt" -noout -subject -issuer -enddate

echo
say "Reloading Nginx so it picks the new certificate up..."
podman exec gateway-nginx-prod nginx -s reload -c /tmp/nginx.conf 2>/dev/null \
    && good "Nginx reloaded" \
    || warn "Could not reload — start the stack, or: podman restart gateway-nginx-prod"

echo
say "${BOLD}Renewal${OFF}: Let's Encrypt certificates last 90 days. Re-run this"
say "command, or add a cron entry — nothing renews on its own here."
echo
