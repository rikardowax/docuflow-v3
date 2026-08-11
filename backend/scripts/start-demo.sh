#!/usr/bin/env bash
# DocuFlow v3 — lance une démo locale sans Docker
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# Désactiver un ancien venv cassé
unset VIRTUAL_ENV || true

if [[ ! -d .venv/bin/python ]]; then
  echo "→ Création du venv..."
  /usr/bin/python3 -m venv .venv
  env -i HOME="$HOME" PATH="/usr/bin:/bin" .venv/bin/pip install --upgrade pip
  env -i HOME="$HOME" PATH="/usr/bin:/bin" PIP_REQUIRE_HASHES=0 .venv/bin/pip install -r requirements.txt
fi

if [[ ! -f .env ]]; then
  echo "→ Copie de .env.example vers .env"
  cp .env.example .env
  echo "   ⚠️  Éditez backend/.env et ajoutez GEMINI_API_KEY pour l'OCR Gemini"
fi

export ENV=development
export RATE_LIMIT_ENABLED=false
export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///./docuflow_dev.db}"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  DocuFlow v3 — Démo locale                          ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Swagger UI : http://localhost:8000/docs             ║"
echo "║  Health     : http://localhost:8000/health           ║"
echo "║  Login      : demo_client / demo_secret              ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

exec env -i HOME="$HOME" PATH="/usr/bin:/bin" \
  ENV="$ENV" RATE_LIMIT_ENABLED="$RATE_LIMIT_ENABLED" DATABASE_URL="$DATABASE_URL" \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
