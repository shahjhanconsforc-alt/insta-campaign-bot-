"""Store campaign media (the pics) in MinIO.

Images are a natural fit for object storage — one image, one object — so each
picture is stored under its own filename in the media bucket. Re-uploading the
same name overwrites it (no duplicate copies), matching how the local media
folder already de-duplicates by name.

    push(name, data, content_type)   store one image
    push_local()                     upload every image in campaign_media/
    load_list()                      [{name, size}] of what's stored
    get(name)                        an image's bytes
    delete(name)

    python -m services.storage.media_storage push   # local pics -> MinIO
    python -m services.storage.media_storage list
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from . import minio_client as mc

BUCKET = mc.BUCKET_MEDIA
log = mc.log


def push(name: str, data: bytes, content_type: str | None = None) -> dict:
    """Store one image under `name` (bare filename; any path part is dropped)."""
    key = Path(str(name)).name
    ctype = content_type or mimetypes.guess_type(key)[0] or "image/jpeg"
    size = mc.put_bytes(BUCKET, key, data, ctype)
    log.info("stored image %r (%d bytes)", key, size)
    return {"name": key, "bytes": size, "bucket": BUCKET}


def get(name: str) -> bytes | None:
    """An image's bytes, or None if it isn't stored."""
    return mc.get_bytes(BUCKET, Path(str(name)).name)


def load_list() -> list[dict]:
    """Every image in the bucket, as [{name, size}]."""
    items = mc.list_objects(BUCKET)
    log.info("%d image(s) in MinIO media bucket", len(items))
    return items


def delete(name: str) -> dict:
    """Remove one image."""
    key = Path(str(name)).name
    mc.remove_object(BUCKET, key)
    log.info("deleted image %r", key)
    return {"deleted": key}


def push_local(folder: Path | None = None) -> dict:
    """Upload every image in the local campaign_media folder. Overwrites by name,
    so re-running never piles up copies."""
    if folder is None:
        from backend import config

        folder = config.BACKEND_DIR / "data" / "campaign_media"
    folder = Path(folder)
    if not folder.is_dir():
        log.warning("no local media folder at %s", folder)
        return {"pushed": 0}

    exts = {".jpg", ".jpeg", ".png"}
    pushed = 0
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in exts:
            push(path.name, path.read_bytes())
            pushed += 1
    log.info("uploaded %d local image(s) to MinIO", pushed)
    return {"pushed": pushed, "bucket": BUCKET}


def _main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    if cmd == "status":
        mc.status()
        return 0
    if not mc.is_up():
        log.error("MinIO is stopped — start it, then try again")
        return 1
    if cmd == "push":
        push_local()
        return 0
    if cmd in ("list", "pull"):
        items = load_list()
        print(f"{len(items)} image(s) in MinIO:")
        for it in items:
            print(f"  - {it['name']}  ({it['size']} bytes)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
