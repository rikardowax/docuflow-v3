# DocuFlow v3 — Démo extraction documentaire

Backend FastAPI + interface web de démo (`/app`).

## Démo locale (sans Docker)

```bash
cd backend
./scripts/start-demo.sh
```

- **Application** : http://localhost:8000/app
- **Swagger** : http://localhost:8000/docs

Identifiants : `demo_client` / `demo_secret`

> Clé API : renseigner `GEMINI_API_KEY` dans `backend/.env`  
> (gratuit sur https://aistudio.google.com/apikey)

## Démo à distance (gratuit, PC allumé)

```bash
cd backend
./scripts/start-remote-demo.sh
```

Partagez le lien `/app` affiché (tunnel Cloudflare).

---

## Déployer sur Render — GRATUIT

URL fixe du type `https://docuflow-v3.onrender.com/app` — **0 €**, sans Docker sur votre PC.

### Limites du plan free Render

| | |
|---|---|
| Coût | **0 €** |
| RAM | 512 Mo → on déploie **extraction seule** (sans PaddleOCR/biométrie) |
| Endormissement | Après 15 min sans visite → réveil en ~30–60 s |
| Disque | SQLite effacé à chaque redéploiement (OK pour démo) |

### Étapes (10 min)

1. **GitHub** — le code doit être sur GitHub  
   Repo : https://github.com/rikardowax/docuflow-v3

2. **Clé Gemini** — https://aistudio.google.com/apikey

3. **Render** — https://render.com → créer un compte (gratuit)

4. **New → Blueprint** (ou **Web Service**)
   - Connecter le repo `docuflow-v3`
   - Render détecte `render.yaml` à la racine
   - Ou manuellement :
     - **Root Directory** : `backend`
     - **Runtime** : Docker
     - **Dockerfile** : `Dockerfile.render`

5. **Variables d'environnement** (obligatoire) :

   | Variable | Valeur |
   |----------|--------|
   | `GEMINI_API_KEY` | votre clé Gemini |
   | `ENV` | `staging` |
   | `DATABASE_URL` | `sqlite+aiosqlite:///./docuflow_dev.db` |
   | `RATE_LIMIT_ENABLED` | `false` |

6. **Plan** : Free → **Create Web Service**

7. Attendre le build (~5–10 min) → ouvrir :
   - **App** : `https://VOTRE-SERVICE.onrender.com/app`
   - **Health** : `https://VOTRE-SERVICE.onrender.com/health`

### Avant une présentation

Le service free s’endort. **1–2 min avant** l’appel, ouvrez `/app` ou `/health` dans le navigateur pour le réveiller.

### Dépannage Render

- **Build échoue (mémoire)** → vérifiez que c’est bien `Dockerfile.render` (pas le Dockerfile complet)
- **Extraction échoue** → vérifiez `GEMINI_API_KEY` dans les env vars Render
- **Page blanche** → allez sur `/app` (pas `/docs` pour la démo utilisateur)

---

## Structure

```
docuflow_v3/
├── backend/
│   ├── app/static/       # Interface web (/app)
│   ├── Dockerfile.render # Image légère pour Render
│   └── requirements-render.txt
├── render.yaml           # Config Render one-click
└── README.md
```
