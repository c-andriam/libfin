#!/usr/bin/env bash
#
#  One-command bootstrap of the libfin gateway in PayMeGate mode on a fresh
#  Ubuntu/Debian VPS (Voie B — persistent 24/7).
#
#  What it does:
#    1. Installs python3-venv and nginx.
#    2. Creates an unprivileged `libfin` user for the gateway.
#    3. Checks out/clones the repo to /opt/libfin.
#    4. Builds the venv and installs the gateway extras.
#    5. Issues a Let's Encrypt certificate for the domain via
#       scripts/duckdns_cert.sh (DNS-01, no inbound port needed).
#    6. Installs the nginx vhost (nginx/paymegate-gateway.example.conf)
#       and redirects HTTP→HTTPS.
#    7. Installs and enables the systemd unit (auto-start on boot).
#    8. Verifies /health and /webhook/paymegate through the public HTTPS URL.
#
#  Required environment (never put secrets in the command line):
#      DOMAIN            e.g. toto.duckdns.org
#      DUCKDNS_TOKEN     DuckDNS token for that domain
#      REPO_URL          git URL of the repo (default: current remote)
#      GATEWAY_API_KEY   the gateway API key (must equal .env's)
#      EMAIL             optional certbot email
#
#  The real secrets (PAYMEGATE_API_KEY, PAYMEGATE_WEBHOOK_SECRET, ...) are NOT
#  in git. Drop your own .env into ${REPO_DIR} afterwards (e.g. scp .env
#  root@vps:/opt/libfin/.env) and restart the unit. With REQUIRE_ENV=1 the
#  script refuses to run until .env is present before the gateway starts.
#
#      sudo DOMAIN=toto.duckdns.org DUCKDNS_TOKEN=... GATEWAY_API_KEY=... \
#           bash deploy/install_vps.sh
#      scp .env root@vps:/opt/libfin/.env
#      ssh root@vps 'systemctl restart libfin-gateway'
#
set -euo pipefail

DOMAIN="${DOMAIN:?Set DOMAIN, e.g. toto.duckdns.org}"
DUCKDNS_TOKEN="${DUCKDNS_TOKEN:?Set DUCKDNS_TOKEN}"
GATEWAY_API_KEY="${GATEWAY_API_KEY:?Set GATEWAY_API_KEY}"
EMAIL="${EMAIL:-}"
REPO_DIR="/opt/libfin"
REPO_URL="${REPO_URL:-$(git -C "$(dirname "$0")/.." remote get-url origin 2>/dev/null || true)}"
REQUIRE_ENV="${REQUIRE_ENV:-}"

BLUE='\033[1;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
say() { echo -e "${BLUE}▸${NC} $*"; }
ok()  { echo -e "  ${GREEN}✔${NC} $*"; }
bad() { echo -e "  ${RED}✘${NC} $*"; }

[[ "${EUID}" -ne 0 ]] && { bad "Run as root (sudo)."; exit 1; }

say "1/8 Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y nginx python3 python3-venv git curl ca-certificates

say "2/8 Creating the libfin service user"
id libfin >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin libfin

say "3/8 Checking out the repo to ${REPO_DIR}"
if [[ -d "${REPO_DIR}/.git" ]]; then
    ok "Already cloned; leaving it in place."
else
    [[ -n "${REPO_URL}" ]] || { bad "REPO_URL not set and no local origin to inherit."; exit 1; }
    git clone "${REPO_URL}" "${REPO_DIR}"
fi
cd "${REPO_DIR}"

say "4/8 Building the Python venv"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -e ".[gateway,test]"
.venv/bin/pip install uvicorn   # not always part of the extras

# Real secrets must exist for a live run. REQUIRE_ENV=1 demands a .env here;
# otherwise we warn but continue (it can be added and the unit restarted).
if [[ -n "${REQUIRE_ENV}" ]]; then
    [[ -f .env ]] || { bad "REQUIRE_ENV=1 but no .env in ${REPO_DIR}."; exit 1; }
    ok ".env present."
else
    [[ -f .env ]] && ok ".env present." || say "No .env yet — add it and restart the unit later."
fi

say "5/8 Issuing the Let's Encrypt certificate (DNS-01 via DuckDNS)"
mkdir -p .certbot-venv
python3 -m venv .certbot-venv
.certbot-venv/bin/pip install --quiet certbot
(cd "${REPO_DIR}" && DUCKDNS_TOKEN="${DUCKDNS_TOKEN}" scripts/duckdns_cert.sh "${DOMAIN}" "${EMAIL}")

say "6/8 Installing the nginx vhost"
mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/ssl
sed -e "s/DOMAIN/${DOMAIN}/g" \
    -e "s/UPSTREAM_PORT/8100/g" \
    -e "s/GATEWAY_API_KEY/${GATEWAY_API_KEY}/g" \
    nginx/paymegate-gateway.example.conf > /etc/nginx/sites-available/libfin-gateway
ln -sf ../sites-available/libfin-gateway /etc/nginx/sites-enabled/libfin-gateway
# Remove the default placeholder site if present.
rm -f /etc/nginx/sites-enabled/default
# The gateway cert/key produced by duckdns_cert.sh.
install -m 644 nginx/ssl/server.crt /etc/nginx/ssl/server.crt
install -m 600 nginx/ssl/server.key /etc/nginx/ssl/server.key
nginx -t
systemctl reload nginx
ok "nginx reloaded."

say "7/8 Installing the systemd unit"
install -m 644 deploy/libfin-gateway.service /etc/systemd/system/libfin-gateway.service
systemctl daemon-reload
chown -R libfin:libfin "${REPO_DIR}" /var/lib/libfin 2>/dev/null || true
systemctl enable --now libfin-gateway
systemctl --no-pager --lines=15 status libfin-gateway || true

say "8/8 Verifying the public endpoint"
sleep 3
curl -fsS "https://${DOMAIN}/health" && echo || bad " /health unreachable — check DNS A record for ${DOMAIN} and that the gateway is up."
curl -s -o /dev/null -w "webhook status: %{http_code}\n" -X POST "https://${DOMAIN}/webhook/paymegate" -H 'Content-Type: application/json' -d '{}'

echo
ok "Done. Configure PayMeGate's webhook URL as:  https://${DOMAIN}/webhook/paymegate"
echo "  (PayMeGate will show 401 here until it sends a properly signed order.paid.)"
