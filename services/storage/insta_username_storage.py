"""Store and read Instagram usernames in MinIO.

The whole username set lives as ONE plain-text object in the bucket —
usernames.txt — one username per line, deduplicated. That mirrors how the local
store.json already thinks about usernames (one deduped set), keeps writes atomic,
and means "access the usernames" is a single fast download rather than listing
thousands of objects. (An older build stored usernames.json; it's read once and
migrated to usernames.txt automatically.)

    push_all(names)   replace the stored set (the one-time migration)
    add(names)        merge new names in, case-insensitive dedup
    load_all()        read the set back
    count()           how many are stored

Run it as a script to migrate the local Data into MinIO:

    python -m services.storage.insta_username_storage push     # local -> MinIO
    python -m services.storage.insta_username_storage list     # show what's stored
    python -m services.storage.insta_username_storage status   # running / stopped

Every operation logs to the terminal via the minio logger.
"""
from __future__ import annotations

import io
import json
import threading

from minio.error import S3Error

from . import minio_client as mc

OBJECT_NAME = "usernames.txt"
_LEGACY_JSON = "usernames.json"   # older format, read once then migrated to .txt

# Read-modify-write (load → merge → push) has to be atomic within this process,
# or two scrapes finishing together could both load, both miss, and one's names
# get overwritten. Single process, so a lock is enough — same guard the local db
# uses around its file.
_lock = threading.Lock()

log = mc.log


def _dedupe(names) -> list[str]:
    """Trim, drop blanks, and remove case-insensitive duplicates, keeping the
    first spelling seen — the same rule the local db uses."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in names or []:
        name = str(raw or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _parse(raw: bytes) -> list[str]:
    """Turn a stored object's bytes into a deduped name list. Handles the plain
    txt format (one per line) and, tolerantly, a legacy JSON payload."""
    text = raw.decode("utf-8")
    head = text.lstrip()[:1]
    if head in ("{", "["):                       # a legacy JSON body
        try:
            payload = json.loads(text)
            names = payload.get("usernames") if isinstance(payload, dict) else payload
            return _dedupe(names if isinstance(names, list) else [])
        except json.JSONDecodeError:
            pass                                  # fall through to line parsing
    return _dedupe(text.splitlines())


def _read_object(name: str):
    """Bytes of `name`, or None if it isn't there. Raises MinioUnavailable on a
    real connection/read failure."""
    client = mc.get_client()
    try:
        response = client.get_object(mc.BUCKET, name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    except S3Error as exc:
        if exc.code in ("NoSuchKey", "NoSuchBucket"):
            return None
        log.error("load failed: %s", exc)
        raise mc.MinioUnavailable(f"MinIO load error: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        log.error("cannot reach MinIO to load usernames (%s)", type(exc).__name__)
        raise mc.MinioUnavailable(
            f"MinIO not reachable ({type(exc).__name__})"
        ) from exc


def load_all() -> list[str]:
    """Every username stored in MinIO. Empty list if nothing's there yet.

    Reads usernames.txt; if only the legacy usernames.json exists, it's read and
    migrated to .txt on the spot. Raises MinioUnavailable if the server is down —
    an empty list would be a lie that could wipe the set on the next push."""
    raw = _read_object(OBJECT_NAME)
    if raw is not None:
        names = _parse(raw)
        log.info("loaded %d username(s) from %s", len(names), OBJECT_NAME)
        return names

    # No .txt yet — migrate the legacy .json if it's there, then use .txt onward.
    legacy = _read_object(_LEGACY_JSON)
    if legacy is not None:
        names = _parse(legacy)
        try:
            _put(names)                          # write usernames.txt
            _delete(_LEGACY_JSON)                # drop the old json so it can't shadow
            log.info("migrated %d username(s) from %s to %s",
                     len(names), _LEGACY_JSON, OBJECT_NAME)
        except mc.MinioUnavailable:
            pass                                 # migration is best-effort
        return names

    log.info("no usernames stored yet (%s)", OBJECT_NAME)
    return []


def _put(names: list[str]) -> int:
    """Write the given set as usernames.txt — one username per line. Overwrites.
    Returns the count."""
    mc.ensure_bucket()
    body = ("\n".join(names) + ("\n" if names else "")).encode("utf-8")
    data = io.BytesIO(body)
    try:
        mc.get_client().put_object(
            mc.BUCKET, OBJECT_NAME, data, length=len(body),
            content_type="text/plain; charset=utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        log.error("write of %s failed (%s)", OBJECT_NAME, type(exc).__name__)
        raise mc.MinioUnavailable(f"MinIO write error: {exc}") from exc
    return len(names)


def _delete(name: str) -> None:
    """Best-effort remove of an object (used to drop the legacy json after
    migration). A failure here is non-fatal."""
    try:
        mc.get_client().remove_object(mc.BUCKET, name)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not remove %s (%s)", name, type(exc).__name__)


def push_all(names) -> dict:
    """Replace the stored set with `names` (deduped). This is the one-time
    migration — it overwrites, so use add() for incremental saves."""
    with _lock:
        cleaned = _dedupe(names)
        total = _put(cleaned)
        log.info("pushed %d username(s) to MinIO", total)
        return {"pushed": total, "bucket": mc.BUCKET, "object": OBJECT_NAME}


def add(names) -> dict:
    """Merge `names` into the stored set, skipping ones already there
    (case-insensitive). Returns the actual `added` and `skipped` name lists (so
    callers can report exactly what landed) plus the new `total`."""
    with _lock:
        incoming = _dedupe(names)
        if not incoming:
            return {"added": [], "skipped": [], "total": count_unsafe()}
        existing = load_all()
        have = {n.lower() for n in existing}
        added = [n for n in incoming if n.lower() not in have]
        skipped = [n for n in incoming if n.lower() in have]
        merged = existing + added
        if added:
            _put(merged)
        log.info("added %d new username(s), skipped %d (total %d)",
                 len(added), len(skipped), len(merged))
        return {"added": added, "skipped": skipped, "total": len(merged)}


def remove(name) -> dict:
    """Delete one username from the stored set (case-insensitive). Returns
    whether it was there."""
    with _lock:
        key = str(name or "").strip().lower()
        existing = load_all()
        kept = [n for n in existing if n.lower() != key]
        removed = len(existing) - len(kept)
        if removed:
            _put(kept)
        log.info("removed %r (%d gone, %d left)", name, removed, len(kept))
        return {"removed": removed, "total": len(kept)}


def count_unsafe() -> int:
    """len(load_all()) without taking the lock — for callers already holding it."""
    try:
        return len(load_all())
    except mc.MinioUnavailable:
        return 0


def count() -> int:
    """How many usernames are stored."""
    return len(load_all())


# ── CLI: migrate local Data -> MinIO, and inspect ─────────────────
def _local_usernames() -> list[str]:
    """The instagram-name records from the running app's local store."""
    from backend import db  # imported here so the module has no hard app dep

    return [
        str(r.get("title") or "").strip()
        for r in db.get_all(10_000_000)
        if r.get("type") == "instagram-name" and str(r.get("title") or "").strip()
    ]


def _main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()

    if cmd == "status":
        mc.status()
        return 0

    if cmd in ("list", "pull"):
        if not mc.is_up():
            return 1
        names = load_all()
        print(f"{len(names)} username(s) in MinIO:")
        for n in names[:50]:
            print("  ", n)
        if len(names) > 50:
            print(f"   … and {len(names) - 50} more")
        return 0

    if cmd == "push":
        if not mc.is_up():
            log.error("MinIO is stopped — start it, then run push again")
            return 1
        local = _local_usernames()
        log.info("read %d username(s) from local Data", len(local))
        # Merge, not overwrite, so re-running push is safe and never loses names.
        result = add(local)
        log.info("migration done — %d new, %d already there, %d total in MinIO",
                 len(result["added"]), len(result["skipped"]), result["total"])
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
