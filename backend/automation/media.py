"""Campaign media store.

Images the Campaign panel attaches are uploaded here (base64 in a JSON POST, so
no python-multipart dependency), saved to a local folder, and handed back to the
browser step as real file paths for Playwright's file chooser.

Kept deliberately small: this is a local, single-user app, so the store is just
a folder under backend/data and the "id" is the saved filename.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

# Only the image types the panel accepts. Instagram DMs take these directly.
ALLOWED_EXT = {"jpg", "jpeg", "png"}
_EXT_FROM_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
}

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _ext_for(name: str, mime: str) -> str:
    """Pick an allowed extension from the filename, falling back to the MIME."""
    suffix = Path(name or "").suffix.lower().lstrip(".")
    if suffix == "jpeg":
        suffix = "jpg"
    if suffix in ALLOWED_EXT:
        return suffix
    return _EXT_FROM_MIME.get((mime or "").lower().strip(), "")


def _decode(data: str) -> bytes:
    """Decode a base64 payload, tolerating a leading `data:...;base64,` prefix."""
    payload = data or ""
    if "," in payload and payload.strip().lower().startswith("data:"):
        payload = payload.split(",", 1)[1]
    return base64.b64decode(payload, validate=False)


def save_uploads(files: list[dict]) -> list[dict]:
    """Save uploaded images and return [{name, stored, bytes}] for each.

    `files` is a list of {name, type, data(base64)}. Non-image types and empty
    payloads are rejected with a ValueError so the panel hears why, rather than
    a half-saved batch.
    """
    if not files:
        raise ValueError("No files to upload.")

    from services.storage import media_storage

    saved: list[dict] = []
    for item in files:
        name = str(item.get("name") or "image")
        ext = _ext_for(name, str(item.get("type") or ""))
        if ext not in ALLOWED_EXT:
            raise ValueError(
                f"{name!r} is not a supported image (only JPG, JPEG, PNG)."
            )
        raw = _decode(str(item.get("data") or ""))
        if not raw:
            raise ValueError(f"{name!r} is empty.")

        # MinIO is the source of truth. Store under the sanitised name so a
        # re-upload of the same name replaces it, exactly as before.
        stored = f"{_SAFE.sub('-', Path(name).stem).strip('-') or 'image'}.{ext}"
        media_storage.push(stored, raw, item.get("type") or None)
        saved.append({"name": name, "stored": stored, "bytes": len(raw)})
    return saved


def listing() -> list[dict]:
    """Every image in MinIO, newest first — what the panel's media section shows.

    Reads from the media bucket, so the preview reflects what's actually stored
    and survives a reload."""
    from services.storage import media_storage

    items = media_storage.load_list()
    items.sort(key=lambda it: it.get("at", 0), reverse=True)
    return [
        {"name": it["name"], "stored": it["name"],
         "bytes": it.get("size", 0), "at": it.get("at", 0)}
        for it in items
    ]


def _materialize(stored_name: str) -> Path:
    """Download one stored image from MinIO into the local cache and return its
    path. Needed wherever a real file is required — the preview route's
    FileResponse and the send step's file chooser. Raises ValueError if the
    image isn't in MinIO; MinioUnavailable propagates if MinIO is down."""
    from services.storage import cache, media_storage

    bare = Path(str(stored_name)).name       # strip any directory component
    if not bare:
        raise ValueError("No media name given.")
    data = media_storage.get(bare)
    if data is None:
        raise ValueError(f"No stored media named {bare!r}.")
    path = cache.cache_path(bare, sub="media")
    path.write_bytes(data)
    return path


def path_of(stored_name: str) -> Path:
    """A real local path for one stored image (materialised from MinIO)."""
    return _materialize(stored_name)


def delete(stored_name: str) -> dict:
    """Remove one stored image from MinIO (and its local cache copy)."""
    from services.storage import cache, media_storage

    bare = Path(str(stored_name)).name
    media_storage.delete(bare)
    cached = cache.cache_path(bare, sub="media")
    if cached.exists():
        cached.unlink()
    return {"deleted": bare}


def resolve(stored_names: list[str]) -> list[str]:
    """Local paths for the given stored images, materialised from MinIO so the
    browser's file chooser has real files to attach. A name missing from MinIO
    raises, so a run never silently drops an image the user attached."""
    return [str(_materialize(raw)) for raw in stored_names or []]
