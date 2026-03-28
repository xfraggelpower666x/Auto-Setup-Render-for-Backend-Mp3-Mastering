# V51 RENDER SETUP

Dieses Paket ist dein deploy-fertiges Render-Setup für das MP3-Mastering-Backend.

## Nach dem Deploy
Render gibt deiner Web Service Instanz eine öffentliche `onrender.com` URL. Diese setzt du dann im Worker als:
`MASTERING_BACKEND_URL`

## Dein bestehender Worker
`https://666soundsdesign-mp3-mastering.fraggelpower666.workers.dev`

## Deploy
1. Paket in ein GitHub Repo legen
2. Render -> New -> Web Service
3. Repo verbinden
4. Secret setzen:
   `MASTERING_BACKEND_TOKEN`
5. Deploy starten

## Render Commands
Build:
`pip install -r requirements.txt`

Start:
`gunicorn src.server:app`
