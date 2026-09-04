#!/usr/bin/env bash
#
# start.sh — Lance le gateway PayMeGate + un tunnel Cloudflare public.
#
# Point d'entrée unique. Usage :
#     ./start.sh
#
# 1. Démarre le gateway FastAPI sur 127.0.0.1:8100 (PayMeGate mode, réel).
# 2. Lance un tunnel Cloudflare "quick" vers ce port et affiche l'URL publique.
# 3. Imprime les étapes exactes à refaire quand le lien change.
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
CF="${CLOUDFLARED:-$HOME/.local/cloudflared/usr/bin/cloudflared}"

cd "$REPO"

if [ ! -f .env ]; then
  echo "  ERREUR : .env introuvable. Copie-le à la racine (clés PayMeGate)." >&2
  exit 1
fi

echo "== 1) Démarrage du gateway (PayMeGate) sur 127.0.0.1:$PORT =="
.venv/bin/python scripts/run_paymegate_local.py --port "$PORT" \
  > /tmp/gateway.log 2>&1 &
GWPID=$!
echo "  gateway pid=$GWPID  (log: /tmp/gateway.log)"

# attendre que le gateway réponde
for i in $(seq 1 30); do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/health"; then break; fi
  sleep 1
done

echo "== 2) Lancement du tunnel Cloudflare =="
if ! command -v "$CF" >/dev/null 2>&1 && [ ! -x "$CF" ]; then
  echo "  ERREUR : cloudflared introuvable ($CF). Télécharge-le ou copie le binaire." >&2
  echo "  Le gateway tourne en local sur http://127.0.0.1:$PORT (sans tunnel)." >&2
  echo "  Gateway pid=$GWPID — Ctrl+C pour arrêter." >&2
  wait "$GWPID"
  exit 1
fi

setsid "$CF" tunnel --url "http://127.0.0.1:$PORT" --protocol http2 \
  > /tmp/tunnel.log 2>&1 < /dev/null &
CFPID=$!
echo "  tunnel pid=$CFPID  (log: /tmp/tunnel.log)"

echo "== 3) Récupération de l'URL publique =="
URL=""
for i in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/tunnel.log 2>/dev/null | head -1 || true)"
  if [ -n "$URL" ]; then break; fi
  sleep 1
done

if [ -n "$URL" ]; then
  echo ""
  echo "============================================================"
  echo "  PUBLIC_URL   : $URL/checkout"
  echo "  WEBHOOK_URL  : $URL/webhook/paymegate"
  echo "============================================================"
  echo ""
  echo "  À chaque nouveau lien, mets à jour :"
  echo "    1) .env            -> PAYMEGATE_RETURN_URL=$URL"
  echo "    2) Dashboard PayMeGate -> webhook = $URL/webhook/paymegate"
else
  echo "  URL non trouvée dans /tmp/tunnel.log — vérifie le log."
fi

echo ""
echo "  Gateway pid=$GWPID , tunnel pid=$CFPID . Ctrl+C ici arrêtera le gateway."
wait "$GWPID"
