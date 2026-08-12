#!/usr/bin/env bash
# Affiche le lien de démo en cours (si le tunnel tourne)
set -euo pipefail

cd "$(dirname "$0")/.."
URL_FILE="$(pwd)/.demo-url"

if [[ -f "$URL_FILE" ]]; then
  echo "Lien de démo : $(cat "$URL_FILE")"
  exit 0
fi

running="$(pgrep -af 'cloudflared tunnel' 2>/dev/null | grep -v pgrep || true)"
if [[ -z "$running" ]]; then
  echo "Aucun tunnel actif."
  echo "Lancez : ./scripts/start-remote-demo.sh"
  exit 1
fi

echo "Tunnel actif mais lien non enregistré."
echo "Relancez : ./scripts/start-remote-demo.sh"
exit 1
