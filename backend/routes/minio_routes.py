"""MinIO status + log feed for the header pill and its terminal drawer."""
from fastapi import APIRouter

router = APIRouter(prefix="/api/minio", tags=["minio"])


@router.get("/status")
async def status(force: bool = False):
    """Running/stopped, plus where it points and how many usernames are stored.
    Cached (see minio_client.health) unless force=1, so the pill can poll cheaply."""
    from services.storage import insta_username_storage as store
    from services.storage import minio_client

    info = minio_client.health(force=force)
    # Only touch the object store for a count when MinIO is actually up, so a
    # stopped server doesn't cost a slow failed download on every poll.
    count = None
    if info["up"]:
        try:
            count = store.count()
        except Exception:  # noqa: BLE001 — count is a nicety, not the point
            count = None
    return {**info, "count": count}


@router.get("/logs")
async def logs(after: int = 0):
    """MinIO log lines with id greater than `after` — the terminal polls this so
    it only pulls what's new. Includes the current up/down so the terminal's
    status line stays in sync without a second request."""
    from services.storage import minio_client

    info = minio_client.health()
    return {"logs": minio_client.recent_logs(after), "up": info["up"],
            "endpoint": info["endpoint"], "bucket": info["bucket"]}
