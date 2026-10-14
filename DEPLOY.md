
# Deploying insta-bot

The stack is two parts: the **app** (FastAPI + static frontend, drives Chromium
via Playwright) and **MinIO** (object storage). The `docker-compose.yml` runs
both together.

## Quick start (Docker — the whole stack)

```bash
# 1. create your env file from the template and fill in the secrets
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
#    set at least MINIO_USER and MINIO_PASSWORD

# 2. build + start the app (uses the shared object store in MINIO_URL)
docker compose up -d --build
#    or, with the bundled MinIO-compatible store when no shared one exists:
#    docker compose --profile with-minio up -d --build

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
- **The desktop-cursor feature needs a real display.** On Windows it uses
  `pywinauto` + `pyautogui`. On Linux `backend/automation/cursor_x11.py` provides
  the same feature with `xdotool`/`xclip` (installed in the image) — it works when
  the container's `DISPLAY` points at a desktop X server, e.g. a webtop desktop
  whose `/tmp/.X11-unix` is shared into the container and `PLAYWRIGHT_HEADLESS=false`.
  Fully headless (no DISPLAY) the feature is unavailable; everything else runs on
  Playwright and is unaffected.
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

## Ubuntu Desktop VM — headed, you SEE the browser (no Docker)

Docker always runs headless (no screen inside a container). To watch the
automation browser on an Ubuntu **Desktop** VM, run the app directly on the
desktop instead:

```bash
# 1. deps, as the normal desktop user — NOT root/sudo (Chrome refuses to run as root)
sudo apt install -y python3-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium            # bundled Chromium for THIS Python
sudo .venv/bin/python -m playwright install-deps chromium   # system libs (once)

# 2. .env — visible window, bundled Chromium
cp .env.example .env
#    PLAYWRIGHT_HEADLESS=false
#    PLAYWRIGHT_CHANNEL=chromium        (or install Google Chrome and use "chrome")
#    MINIO_URL=...                       (point at your MinIO)

# 3. start it from a terminal INSIDE the desktop session (so DISPLAY is set)
python -m backend.main
```

Why these matter:

- **No display** (`Missing X server or $DISPLAY`): the server was started over SSH,
  from a container, or as a system service. Those shells have no `DISPLAY`. Open a
  terminal on the VM's desktop and start it there, or export the desktop user's
  `DISPLAY=:0` (and `XAUTHORITY`) in the service unit.
- **`Executable doesn't exist`**: Playwright's Chromium is not downloaded for the
  Python you are running. Run `python -m playwright install chromium` with the same
  interpreter (inside the venv). Switching `PLAYWRIGHT_CHANNEL` to `chrome` instead
  needs Google Chrome installed on the VM.
- **`Running as root without --no-sandbox`**: run as a regular user, not root.
- **Autostart with the desktop** (optional): add `python -m backend.main` as a
  Startup Application in GNOME, which runs it inside the graphical session with
  `DISPLAY` already set.
- The desktop-cursor feature works here too: on Linux it uses `xdotool`/`xclip`
  (`backend/automation/cursor_x11.py`), so run `sudo apt install -y xdotool xclip x11-utils`.

## Running the app without Docker

```bash
pip install -r requirements.txt
python -m playwright install chromium      # if not already installed
cp .env.example .env                       # then fill it in
python -m backend.main                     # serves UI + API on $HOST:$PORT
```
