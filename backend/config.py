"""Central config — every .env value is read here and nowhere else."""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

load_dotenv(ROOT_DIR / ".env")


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", 4000))
ENV = os.getenv("ENV", "development")

DB_FILE = BACKEND_DIR / os.getenv("DB_FILE", "data/store.json")

PLAYWRIGHT_HEADLESS = _bool("PLAYWRIGHT_HEADLESS", False)
PLAYWRIGHT_CHANNEL = os.getenv("PLAYWRIGHT_CHANNEL", "chrome")
# Where "Open Chrome" lands when no URL is given. Empty = blank window.
PLAYWRIGHT_START_URL = os.getenv("PLAYWRIGHT_START_URL", "").strip()

IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "")
PLAYWRIGHT_SLOWMO = int(os.getenv("PLAYWRIGHT_SLOWMO", 0))

# Persistent Chrome profile — keeps you logged in between runs.
PLAYWRIGHT_USER_DATA_DIR = BACKEND_DIR / os.getenv("PLAYWRIGHT_USER_DATA_DIR", "data/chrome-profile")
# Strip the "controlled by automated test software" infobar + navigator.webdriver.
PLAYWRIGHT_CLEAN_LAUNCH = _bool("PLAYWRIGHT_CLEAN_LAUNCH", True)

# ─── Proxy (optional) ──────────────────────────────────────────────────────
# Route the browser through a residential/mobile proxy so automation doesn't run
# from a flagged datacenter IP (the single biggest bot-detection tell). Set ONE
# proxy — it's applied at launch and kept for the whole session: Instagram treats
# an IP change on a logged-in session as a "suspicious login", so we deliberately
# do NOT rotate mid-session. Leave PROXY_SERVER empty to run without a proxy.
#   PROXY_SERVER e.g. http://host:port  or  socks5://host:port
PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")
# Comma-separated hosts that skip the proxy (e.g. "localhost,127.0.0.1").
PROXY_BYPASS = os.getenv("PROXY_BYPASS", "").strip()


def playwright_proxy() -> dict | None:
    """The `proxy=` dict Playwright's launch wants, or None when no proxy is set.

    Kept here so .env reading stays in one place; the runner just passes the
    result straight to launch_persistent_context()."""
    if not PROXY_SERVER:
        return None
    proxy: dict = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        proxy["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        proxy["password"] = PROXY_PASSWORD
    if PROXY_BYPASS:
        proxy["bypass"] = PROXY_BYPASS
    return proxy
