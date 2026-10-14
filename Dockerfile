# insta-bot — FastAPI backend + static frontend, with Playwright/Chromium.
#
# Base image ships Python + Playwright + the browser builds preinstalled, and the
# tag is pinned to the same Playwright version as requirements.txt so the library
# and the bundled browsers never drift apart.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# X11 tools for the desktop-cursor feature on Linux (backend/automation/cursor_x11.py):
# real pointer/keyboard input via XTEST, clipboard read-back, window lookup. They
# only do something when DISPLAY points at a desktop (e.g. a shared webtop display).
RUN apt-get update \
 && apt-get install -y --no-install-recommends xdotool xclip x11-utils \
 && rm -rf /var/lib/apt/lists/*

# Deps first so this layer is cached until requirements.txt actually changes.
# pywinauto/pyautogui are marked win32-only in requirements.txt, so pip skips
# them here (they need a Windows desktop; the desktop-cursor feature is off in a
# container, everything else runs on Playwright which works headless).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Google Chrome stable alongside the bundled Chromium: profiles saved from a desktop
# Chrome (newer than Playwright's Chromium) only open in the same browser generation.
RUN python -m playwright install --with-deps chrome && rm -rf /var/lib/apt/lists/*

# App code (frontend/ is served by the same server — see backend/main.py).
COPY . .

# In a container there is no display: run headless with the bundled Chromium,
# and bind to all interfaces so the published port is reachable.
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=4000 \
    ENV=production \
    PLAYWRIGHT_HEADLESS=true \
    PLAYWRIGHT_CHANNEL=chrome

EXPOSE 4000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "4000"]
