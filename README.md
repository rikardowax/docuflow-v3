# DocuFlow v3 — Démo API

Plateforme d'extraction documentaire (OCR Gemini, biométrie, validation) — backend FastAPI.

## Démo rapide (sans Docker)

```bash
cd backend
./scripts/start-demo.sh
```

Puis ouvrir :
- **Swagger UI** : http://localhost:8000/docs
- **Health check** : http://localhost:8000/health

### Identifiants de démo

| Champ | Valeur |
|-------|--------|
| `client_id` | `demo_client` |
| `client_secret` | `demo_secret` |

### Scénario de présentation (5 min)

1. Ouvrir `/docs` et montrer la liste des endpoints.
2. **Auth** → `POST /v2/auth/token` avec le body JSON :
   ```json
   {"client_id":"demo_client","client_secret":"demo_secret","grant_type":"client_credentials"}
   ```
   → copier le JWT.
3. Cliquer **Authorize** (cadenas) → coller `Bearer <token>`.
4. **OCR Gemini** → `POST /v2/ocr/gemini` → uploader une photo de CNI/passeport.
5. **Stats** → `GET /v2/stats` pour montrer le monitoring.

> **Gemini OCR** : renseigner `GEMINI_API_KEY` dans `backend/.env` (clé gratuite sur https://aistudio.google.com/apikey).

## Présentation à distance

### Option A — Tunnel local (sans Docker, recommandé pour démo immédiate)

Un seul terminal suffit :

```bash
cd backend
chmod +x scripts/start-remote-demo.sh
./scripts/start-remote-demo.sh
```

Le script affiche une URL publique du type `https://xxxx.trycloudflare.com/docs` — partagez-la à votre interlocuteur.

Alternative manuelle (2 terminaux) :

```bash
# Terminal 1
cd backend && ./scripts/start-demo.sh

# Terminal 2
cloudflared tunnel --url http://localhost:8000
```

### Option B — Render.com (recommandé, URL publique permanente)

1. Poussez ce repo sur GitHub.
2. Créez un compte sur [render.com](https://render.com) → **New Web Service**.
3. Connectez le repo, choisissez **Docker**, répertoire `backend/`.
4. Variables d'environnement :
   - `ENV=staging`
   - `GEMINI_API_KEY=<votre_clé>`
   - `DATABASE_URL=sqlite+aiosqlite:///./docuflow_dev.db`
   - `RATE_LIMIT_ENABLED=false`
5. Déployez → URL publique du type `https://docuflow-xxxx.onrender.com/docs`.

## Structure

```
docuflow_v3/
├── backend/          # API FastAPI
│   ├── app/          # Code source
│   ├── Dockerfile    # Pour Render / CI
│   └── scripts/      # start-demo.sh
└── .github/          # CI/CD
```

## Licence

Projet interne — usage démo.
