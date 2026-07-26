"""FastAPI entrypoint.  Run:  python -m backend.main   (or: uvicorn backend.main:app --reload)"""
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import config, db
from backend.automation import playwright_runner as browser
from backend import activity, sessions
from backend.routes import (
    accounts_routes,
    campaign_routes,
    activity_routes,
    agent_routes,
    automation_routes,
    data_routes,
    minio_routes,
    settings_routes,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    activity.log("server", f"insta-bot started on {config.HOST}:{config.PORT}", "ok")
    # Sessions expire while we're closed, so trust disk over whatever the store
    # last recorded. Reads cookie files only — no browser, ~5ms per account.
    try:
        sessions.sweep()
    except Exception as exc:  # noqa: BLE001 — never let this stop the server booting
        activity.log("sessions", f"Startup sweep failed: {exc}", "error")
    # Print MinIO's running/stopped state to the terminal on boot. Best-effort:
    # a stopped MinIO must never stop the app from starting — is_up() just logs
    # and returns False, and the storage layer stays dormant until it's back.
    try:
        from services.storage import minio_client
        minio_client.is_up()
    except Exception as exc:  # noqa: BLE001
        activity.log("minio", f"MinIO status check skipped: {exc}", "warn")
    yield
    await browser.close_browser()  # don't leak a Chrome process on shutdown


app = FastAPI(title="insta-bot agent", version="1.0.0", lifespan=lifespan)

# Which panel a given endpoint belongs to — so the Activity feed reads in the
# user's terms ("accounts") rather than the router's ("/api/accounts/login").
_SOURCES = {
    "agent": "agent",
    "data": "data",
    "automation": "browser",
    "accounts": "accounts",
    "settings": "config",
}

# Chatty endpoints the panels poll on a timer. Logging these would bury the
# real events under a stream of health checks.
_QUIET = {"/api/status", "/api/accounts", "/api/activity"}


@app.middleware("http")
async def log_requests(request, call_next):
    path = request.url.path
    # never log the activity endpoints themselves — that feeds back on itself
    if not path.startswith("/api/") or path.startswith("/api/activity"):
        return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - started) * 1000)

    quiet = path in _QUIET and response.status_code < 400
    if quiet and request.method == "GET":
        return response

    parts = path.split("/")
    source = _SOURCES.get(parts[2] if len(parts) > 2 else "", "http")
    level = "error" if response.status_code >= 500 else "warn" if response.status_code >= 400 else "ok"
    activity.log(
        source,
        f"{request.method} {path} → {response.status_code}",
        level,
        ms=ms,
        status=response.status_code,
    )
    return response


app.include_router(agent_routes.router)
app.include_router(data_routes.router)
app.include_router(campaign_routes.router)
app.include_router(automation_routes.router)
app.include_router(accounts_routes.router)
app.include_router(settings_routes.router)
app.include_router(activity_routes.router)
app.include_router(minio_routes.router)


@app.get("/api/status")
async def status():
    return {
        "status": "ready",
        "env": config.ENV,
        "db": db.status(),
        "browser": await browser.status(),
    }


_SHOTS = config.BACKEND_DIR / "data" / "screenshots"
_SHOTS.mkdir(parents=True, exist_ok=True)
app.mount("/shots", StaticFiles(directory=_SHOTS), name="shots")


@app.middleware("http")
async def _no_cache_frontend(request, call_next):
    """Stop the browser caching the UI.

    Without this there's no Cache-Control at all, so browsers fall back to
    heuristic caching and keep serving an old app.js from cache without ever
    asking whether it changed — an edit then looks like it did nothing until a
    hard refresh. The API is tiny and the UI is local, so never cache either.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/shots"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/")
async def index():
    return FileResponse(config.FRONTEND_DIR / "index.html")


# Mounted last so it never shadows /api/*.
app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
