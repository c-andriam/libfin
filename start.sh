#!/usr/bin/env bash
#
# start.sh — Lance le gateway PayMeGate + un tunnel Cloudflare public.
#
# Point d'entrée unique. Usage :
#     ./start.sh
#
# 1. Démarre le gateway FastAPI sur 127.0.0.1:8100 (PayMeGate mode, réel).
# 2. Lance un tunnel Cloudflare "quick" vers ce port et affiche l'URL publique.
# 3. Met à jour automatiquement PAYMEGATE_RETURN_URL dans .env.
#
# Pré-requis (nouvelle machine) :
#   python3 -m venv .venv
#   .venv/bin/pip install -e '.[gateway,test]'
#   # copier .env à la racine (contient les vraies clés PayMeGate)
#   # cloudflared : re-télécharger ou copier ~/.local/cloudflared/usr/bin/cloudflared
#
set -euo pipefail

PORT="${PORT:-8100}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CF="${CLOUDFLARED:-$(command -v cloudflared 2>/dev/null || echo "$HOME/.local/cloudflared/usr/bin/cloudflared")}"

cd "$REPO"

if [ ! -f .env ]; then
  echo "ERREUR : .env introuvable. Copie-le a la racine (cles PayMeGate)." >&2
  exit 1
fi

# ── Nettoyage : tuer un eventuel ancien gateway/tunnel avant de demarrer ─────
echo "== 0) Nettoyage des anciens processus =="
if pgrep -f run_paymegate_local.py >/dev/null 2>&1; then
  echo "  arret de l'ancien gateway..."
  pkill -f run_paymegate_local.py 2>/dev/null || true
fi
if pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
  echo "  arret des anciens tunnels..."
  pkill -f "cloudflared tunnel" 2>/dev/null || true
fi
# laisser le temps aux anciens processus de liberer le port
for i in $(seq 1 10); do
  if ! ss -tlnp 2>/dev/null | grep -q ":$PORT "; then break; fi
  sleep 1
done
sleep 1
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
  echo "ERREUR : le port $PORT est toujours occupe." >&2
  ss -tlnp 2>/dev/null | grep ":$PORT " >&2
  exit 1
fi

echo "== 1) Demarrage du gateway sur 127.0.0.1:$PORT =="
.venv/bin/python scripts/run_paymegate_local.py --port "$PORT" \
  > /tmp/gateway.log 2>&1 &
GWPID=$!
echo "  pid=$GWPID"

# attendre que le gateway reponde, sinon verifier s'il est mort
for i in $(seq 1 30); do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/health"; then break; fi
  if ! kill -0 "$GWPID" 2>/dev/null; then
    echo "ERREUR : le gateway s'est arrete au demarrage." >&2
    echo "  Dernieres lignes de /tmp/gateway.log :" >&2
    tail -15 /tmp/gateway.log >&2
    exit 1
  fi
  sleep 1
done

echo "== 2) Demarrage du tunnel Cloudflare =="
if ! command -v "$CF" >/dev/null 2>&1 && [ ! -x "$CF" ]; then
  echo "ERREUR : cloudflared introuvable ($CF)." >&2
  echo "  Le gateway tourne en local sur http://127.0.0.1:$PORT (sans tunnel)." >&2
  wait "$GWPID"
  exit 1
fi

setsid "$CF" tunnel --url "http://127.0.0.1:$PORT" --protocol http2 \
  > /tmp/tunnel.log 2>&1 < /dev/null &
CFPID=$!

echo "== 3) Recuperation du lien public =="
URL=""
for i in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/tunnel.log 2>/dev/null | head -1 || true)"
  if [ -n "$URL" ]; then break; fi
  sleep 1
done

if [ -n "$URL" ]; then
  # Mettre a jour PAYMEGATE_RETURN_URL dans .env
  if grep -q "^PAYMEGATE_RETURN_URL=" .env; then
    sed -i "s|^PAYMEGATE_RETURN_URL=.*|PAYMEGATE_RETURN_URL=$URL|" .env
  else
    echo "PAYMEGATE_RETURN_URL=$URL" >> .env
  fi

  echo ""
  echo "============================================================"
  echo ""
  echo "  Ouvre ce lien dans ton navigateur pour voir la page client :"
  echo ""
  echo "  >>>  $URL/checkout  <<<"
  echo ""
  echo "============================================================"
  echo ""
  echo "  Le .env a ete mis a jour automatiquement."
  echo ""
  echo "  Webhook a copier dans le dashboard PayMeGate :"
  echo "  $URL/webhook/paymegate"
else
  echo "  Lien non trouve — verification de /tmp/tunnel.log ..."
fi

echo ""
echo "  Ctrl+C = arreter. Gateway pid=$GWPID, tunnel pid=$CFPID."
wait "$GWPID"
