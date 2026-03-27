# DEPLOY STEPS

1. GitHub Repo mit diesem Paket anlegen
2. Render öffnen
3. New -> Web Service
4. Repo wählen
5. Build Command:
   pip install -r requirements.txt
6. Start Command:
   gunicorn src.server:app
7. Secret setzen:
   MASTERING_BACKEND_TOKEN=DEIN_TOKEN
8. Deploy
9. Ergebnis-URL in den Worker eintragen:
   MASTERING_BACKEND_URL=https://DEIN-SERVICE.onrender.com

Worker bleibt:
https://666soundsdesign-mp3-mastering.fraggelpower666.workers.dev
