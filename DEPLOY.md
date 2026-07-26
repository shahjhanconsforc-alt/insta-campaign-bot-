
# Deploying insta-bot

The stack is two parts: the **app** (FastAPI + static frontend, drives Chromium
via Playwright) and **MinIO** (object storage). The `docker-compose.yml` runs
both together.

## Quick start (Docker — the whole stack)

```bash
# 1. create your env file from the template and fill in the secrets
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
#    set at least MINIO_USER and MINIO_PASSWORD

# 2. build + start app, minio
docker compose up -d --build

# 3. open the UI
#    app      → http://localhost:4000
#    minio    → http://localhost:9001  (console)
```

Stop with `docker compose down` (add `-v` to also wipe the data volumes).

## Configuration

Everything is driven by env vars — see **`.env.example`** for the full list with
comments. Compose reads `MINIO_USER` / `MINIO_PASSWORD` from `.env`, so the same
credentials seed the MinIO server and the app. Inside Compose the app talks to
`minio:9000` (the service name), which the compose file already sets for you —
you don't put that in `.env`.

## Important caveats (read before deploying)

This project was built to run on a **local desktop**. A container/server deploy
works for the API and scraping, but comes with real limits:

- **Headless only.** In a container there is no screen, so Playwright runs
  headless (`PLAYWRIGHT_HEADLESS=true`, `PLAYWRIGHT_CHANNEL=chromium`). You can't
  watch the Chrome window the way you can locally.
- **The desktop-cursor feature is disabled.** The address-bar automation in
  `backend/automation/cursor.py` uses `pywinauto` + `pyautogui`, which are
  **Windows-desktop only** (skipped by `pip` on Linux via the markers in
  `requirements.txt`). Everything else runs on Playwright and is unaffected.
- **Instagram will challenge datacenter IPs.** Automated logins from a cloud/server
  IP are far more likely to hit 2FA / "suspicious login" than from a home desktop.
  Expect to finish challenges manually, and prefer a persistent logged-in session.
  Set `PROXY_SERVER` (+ `PROXY_USERNAME` / `PROXY_PASSWORD`) in `.env` to route the
  browser through a **residential/mobile proxy** — the single most effective step.
  Use one stable proxy per session (the app never rotates mid-session, since an IP
  change on a logged-in account itself reads as suspicious). For multiple accounts,
  give each its own residential IP.

If you only want to host the **dependency** and keep running the app on your
Windows machine, run just that service:

```bash
docker compose up -d minio
# then locally:  set MINIO_URL=localhost:9000 in .env
python -m backend.main
```

## Running the app without Docker

```bash
pip install -r requirements.txt
python -m playwright install chromium      # if not already installed
cp .env.example .env                       # then fill it in
python -m backend.main                     # serves UI + API on $HOST:$PORT
```
