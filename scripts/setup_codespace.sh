#!/usr/bin/env bash
#
# setup_codespace.sh — Mise en route du gateway dans un GitHub Codespace.
#
# Usage (dans le terminal du Codespace) :
#     bash scripts/setup_codespace.sh
#
# 1. Crée le .env minimal PayMeGate-mode si absent (demande les 2 secrets).
# 2. Installe les dépendances Python + cloudflared.
# 3. Lance start.sh → gateway + tunnel Cloudflare public + retour auto.
#
# Les secrets restent locaux au Codespace : rien n'est poussé sur GitHub.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "== 1) Fichier .env =="
if [ ! -f .env ]; then
  read -r -p "  PAYMEGATE_API_KEY (pmg_live_...) : " PAYMEGATE_API_KEY
  read -r -p "  PAYMEGATE_WEBHOOK_SECRET          : " PAYMEGATE_WEBHOOK_SECRET
  cat > .env <<EOF
# ====== .env minimal PayMeGate-mode (genere par setup_codespace.sh) ======
# La clé API et le secret webhook sont fournis interactivement ci-dessous.
# Ce fichier est gitignore — jamais poussé sur le remote.
ACQUIRER=paymegate
GATEWAY_MODE=simulation
ENVIRONMENT=development
DATABASE_URL=sqlite+aiosqlite:///$HOME/.local-gateway-paymegate.db
AUTO_CREATE_SCHEMA=true
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0

PAYMEGATE_BASE_URL=https://api.paymegate.com
PAYMEGATE_API_KEY=$PAYMEGATE_API_KEY
PAYMEGATE_WEBHOOK_SECRET=$PAYMEGATE_WEBHOOK_SECRET
PAYMEGATE_PAYMENT_METHODS=["banxa","topper","alchemy","simplex","moonpay"]
PAYMEGATE_RETURN_URL=

GATEWAY_API_KEY=local-paymegate-test-key
CORS_ORIGINS=*
RATE_LIMIT_PER_MINUTE=1000
AMOUNT_MIN=0.01
AMOUNT_MAX=10000.00

WEB3_RPC_URL=
RATE_SOURCE=fixed
EXCHANGE_RATE=1.0
RATE_TOKEN_SYMBOL=USDT
RATE_SETTLEMENT_CURRENCY=USD
RATE_SPREAD=0.00
RATE_MAX_MOVEMENT=1.0
CIRCUIT_BREAKER_FAIL_CLOSED=false
CIRCUIT_BREAKER_THRESHOLD=3
CIRCUIT_BREAKER_RECOVERY_SEC=30
EOF
  echo "  .env cree."
else
  echo "  .env deja present — il sera utilise tel quel."
fi
chmod 600 .env

echo "== 2) Dependances =="
python3 -m venv .venv
.venv/bin/pip install -q -e '.[gateway,test]'

echo "== 3) cloudflared =="
if ! command -v cloudflared >/dev/null 2>&1; then
  sudo apt-get update -q && sudo apt-get install -y -q cloudflared
fi

echo "== 4) Lancement (start.sh) =="
exec ./start.sh