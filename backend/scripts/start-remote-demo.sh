#!/usr/bin/env bash
# DocuFlow v3 — démo accessible à distance (sans Docker, sans compte ngrok)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
URL_FILE="$ROOT/.demo-url"

CLOUDFLARED="${CLOUDFLARED:-/tmp/cloudflared}"
if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "→ Téléchargement de cloudflared..."
  curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o "$CLOUDFLARED"
  chmod +x "$CLOUDFLARED"
fi

print_demo_url() {
  local base="$1"
  local app_url="${base}/app"
  printf '%s\n' "$app_url" > "$URL_FILE"
  echo ""
  echo "════════════════════════════════════════════════════════════"
  echo "  ✅ LIEN À PARTAGER (Application) :"
  echo ""
  echo "     $app_url"
  echo ""
  echo "  Swagger : ${base}/docs"
  echo "  Health  : ${base}/health"
  echo "  (copié aussi dans $URL_FILE)"
  echo "════════════════════════════════════════════════════════════"
  echo ""
}

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  DocuFlow v3 — Démo distante                                ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Login : demo_client / demo_secret                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Lance l'API en arrière-plan si elle n'est pas déjà up
if ! curl -sf http://localhost:8000/health >/dev/null 2>&1; then
  echo "→ Démarrage de l'API locale (peut prendre 1–2 min la 1ère fois)..."
  ./scripts/start-demo.sh >"$ROOT/.demo-api.log" 2>&1 &
  API_PID=$!
  trap 'kill $API_PID 2>/dev/null || true' EXIT

  for i in $(seq 1 90); do
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
      echo "→ API prête sur http://localhost:8000"
      break
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
      echo "❌ L'API n'a pas démarré. Voir $ROOT/.demo-api.log"
      tail -20 "$ROOT/.demo-api.log" || true
      exit 1
    fi
    if (( i % 5 == 0 )); then
      echo "   … attente API (${i}s)"
    fi
    sleep 1
  done

  if ! curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    echo "❌ Timeout : l'API ne répond pas. Voir $ROOT/.demo-api.log"
    exit 1
  fi
else
  echo "→ API déjà active sur http://localhost:8000"
fi

echo "→ Ouverture du tunnel public (le lien apparaît dans ~10 s)..."
echo ""

URL_PRINTED=0
"$CLOUDFLARED" tunnel --url http://localhost:8000 2>&1 | while IFS= read -r line; do
  printf '%s\n' "$line"

  if [[ "$URL_PRINTED" -eq 0 ]]; then
    tunnel_base="$(printf '%s\n' "$line" | grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' | head -1 || true)"
    if [[ -n "$tunnel_base" ]]; then
      print_demo_url "$tunnel_base"
      URL_PRINTED=1
    fi
  fi
done
