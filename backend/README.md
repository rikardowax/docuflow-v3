DocuFlow v3 — Guide pour démo distante
=====================================

But: Backend de DocuFlow v3 (FastAPI). Ce README décrit comment préparer le dépôt, lancer une démo locale et déployer une image Docker sur Render (option recommandée pour une présentation à distance).

Résumé rapide
-------------
- Démo prête à l'emploi via Render (image Docker incluse dans le repo).
- Credentials de démonstration pour login API (dev): client_id: `demo_client`, secret: `demo_secret`.
- Endpoints utiles:
  - /health  -> état
  - /docs    -> Swagger UI (présent si ENV != production)
  - /v2/*    -> API principale

Ce que j'ai préparé
-------------------
- .gitignore: ignore .env, .venv, la DB locale et les modèles volumineux.
- Dockerfile: déjà présent et prêt — installe Tesseract et dépendances système.
- README.md (celui-ci): instructions pour pousser sur GitHub et déployer sur Render.

1) Vérifications et variables d'environnement
--------------------------------------------
Copier .env.example vers .env et adapter si nécessaire:

  cp .env.example .env
  # Éditez .env et renseignez les variables (notamment DB/REDIS/SENTRY si besoin)

Remarques importantes pour la démo:
- Le fichier `docuflow_dev.db` est une DB SQLite de développement; ne pas le committer.
- Le projet dépend de Tesseract et de paquets système (Dockerfile les installe). Pour exécuter sans Docker, installez tesseract localement (ex: sudo apt install tesseract-ocr tesseract-ocr-fra libmagic1 ...).

2) Exécuter localement (option « sans Docker »)
-----------------------------------------------
(utile pour tests rapides si vous avez les dépendances système installées):

  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  cp .env.example .env
  # modifier .env si besoin
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Puis ouvrir http://localhost:8000/health et http://localhost:8000/docs

3) Préparer, créer le repo GitHub et pousser (commande à exécuter sur votre PC)
-------------------------------------------------------------------------------
Exemples de commandes (remplacer <USERNAME> et <REPO> par vos valeurs):

  git init
  git add .
  git commit -m "Initial commit: DocuFlow backend (demo-ready)"
  # Option A: si vous avez GitHub CLI (gh) installé — plus simple
  gh repo create <USERNAME>/<REPO> --public --source=. --remote=origin --push

  # Option B: sans gh — créez le repo via l'interface web GitHub puis:
  git branch -M main
  git remote add origin https://github.com/<USERNAME>/<REPO>.git
  git push -u origin main

N.B. : ne partagez pas votre token GitHub ici. Si vous utilisez un token, exécutez les commandes localement et saisissez le token quand git le demande.

4) Déploiement sur Render (recommandé pour démo distante)
--------------------------------------------------------
Render propose de déployer directement depuis GitHub en utilisant le Dockerfile:

- Créer un compte sur https://render.com et connecter votre compte GitHub.
- Dans le tableau de bord Render -> New -> Web Service.
  - Connectez le repo que vous venez de pousser.
  - Choose "Docker" as the environment (Render detectera le Dockerfile à la racine).
  - Set the port to 8000 (Render forwards HTTP to container port).
  - Add environment variables (ENV, DB connection strings, REDIS URL, SENTRY_DSN if used). At minimum, ensure ENV is not set to "production" if you want /docs accessible (or set to 'staging').
  - Leave the build/start command empty (Dockerfile defines CMD).
- Deploy: Render builds the Docker image and déploie l'application. Une URL publique sera créée (ex: https://your-app.onrender.com).

Vérifiez ensuite:
  https://<your-app>.onrender.com/health
  https://<your-app>.onrender.com/docs

5) Conseils pour la démo à distance
----------------------------------
- Préparez à l'avance l'URL Render et testez les endpoints.
- Utilisez les identifiants demo_client/demo_secret pour montrer un exemple d'authentification si nécessaire.
- Si vous voulez montrer l'UI Swagger, assurez-vous que ENV n'est pas `production` avant le déploiement (ou copiez /docs vers un chemin public temporaire).

6) Si vous voulez que je prépare autre chose
-------------------------------------------
Options possibles (dites laquelle):
- Ajouter un badge Docker/Build dans le README.
- Préparer un template GitHub Actions pour CI (tests + build image).
- Préparer un script shell que vous exécuterez localement pour créer le repo et pousser automatiquement (ce script utilisera votre token sur votre machine).

Questions / Suivant
-------------------
Dites si vous souhaitez que j'ajoute un template GitHub Actions ou un script d'automatisation local. Je peux aussi vous fournir des instructions pas-à-pas en 1‑ligne pour la création du repo si besoin.
