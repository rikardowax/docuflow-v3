#!/usr/bin/env bash
# DocuFlow v3 — démo accessible à distance (sans Docker, sans compte ngrok)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

CLOUDFLARED="${CLOUDFLARED:-/tmp/cloudflared}"
if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "→ Téléchargement de cloudflared..."
  curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o "$CLOUDFLARED"
  chmod +x "$CLOUDFLARED"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  DocuFlow v3 — Démo distante                                ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  1. Ce script lance l'API + un tunnel Cloudflare             ║"
echo "║  2. Partagez l'URL https://xxxx.trycloudflare.com/docs       ║"
echo "║  3. Login : demo_client / demo_secret                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Lance l'API en arrière-plan si elle n'est pas déjà up
if ! curl -sf http://localhost:8000/health >/dev/null 2>&1; then
  echo "→ Démarrage de l'API locale..."
  ./scripts/start-demo.sh &
  API_PID=$!
  trap 'kill $API_PID 2>/dev/null || true' EXIT

  for _ in $(seq 1 60); do
    curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
    sleep 2
  done
else
  echo "→ API déjà active sur http://localhost:8000"
fi

echo "→ Ouverture du tunnel public..."
exec "$CLOUDFLARED" tunnel --url http://localhost:8000
