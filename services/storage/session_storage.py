"""Back up and restore logged-in account sessions in MinIO.

A "session" is an account's Chrome profile directory — the sessionid cookie and
the local-storage tokens that keep it logged in live there. Profiles run to
hundreds of MB, but almost all of that is disposable cache, so each profile is
zipped WITHOUT its cache directories and stored as one object per profile:

    session-<profile>.zip        e.g. session-acct-131.zip

    backup(profile)      one profile -> MinIO
    backup_all()         every profile under data/profiles/
    restore(profile)     MinIO -> local profile dir
    list_sessions()      what's backed up, with sizes

    python -m services.storage.session_storage backup     # all profiles -> MinIO
    python -m services.storage.session_storage list

Backing up a profile Chrome is *running* on is best-effort: locked files (the
live Cookies db) are skipped with a warning rather than failing the whole zip —
close the window first for a complete backup. Restore refuses to overwrite a
non-empty profile unless you pass overwrite=True, so it can't clobber a live
login by accident.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from . import minio_client as mc

BUCKET = mc.BUCKET_SESSIONS
log = mc.log

# Directories that are pure cache — excluded from the backup. Matched by name
# anywhere in the profile tree, case-insensitively. Everything else (cookies,
# Local Storage, Login Data, Preferences, Local State) is kept.
_CACHE_DIRS = {
    "cache", "code cache", "gpucache", "shadercache", "grshadercache",
    "dawncache", "dawngraphitecache", "dawnwebgpucache", "scriptcache",
    "cachestorage", "application cache", "component_crx_cache",
    "extensions_crx_cache", "optimization_guide_model_store",
}


def _obj_name(profile: str) -> str:
    return f"session-{Path(profile).name}.zip"


def _profiles_dir() -> Path:
    from backend import config

    return config.BACKEND_DIR / "data" / "profiles"


def _zip_profile(profile_dir: Path) -> tuple[bytes, int, int]:
    """Zip a profile into memory, skipping cache dirs and any locked files.
    Returns (zip_bytes, files_added, files_skipped)."""
    buf = io.BytesIO()
    added = skipped = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in profile_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(profile_dir)
            # Skip if any path segment is a cache directory.
            if any(part.lower() in _CACHE_DIRS for part in rel.parts):
                continue
            try:
                zf.writestr(str(rel).replace("\\", "/"), path.read_bytes())
                added += 1
            except (PermissionError, OSError) as exc:
                # Locked by a running Chrome, or vanished mid-walk. Skip it and
                # keep going — a partial backup beats no backup.
                log.warning("skipped locked/unreadable file %s (%s)",
                            rel, getattr(exc, "strerror", None) or type(exc).__name__)
                skipped += 1
    return buf.getvalue(), added, skipped


def backup(profile: str) -> dict:
    """Zip one profile (by its directory name, e.g. 'acct-131') and store it."""
    profile_dir = _profiles_dir() / Path(profile).name
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"no profile directory at {profile_dir}")
    data, added, skipped = _zip_profile(profile_dir)
    name = _obj_name(profile)
    size = mc.put_bytes(BUCKET, name, data, "application/zip")
    log.info("backed up %s -> %s (%d files, %d skipped, %.1f MB)",
             profile, name, added, skipped, size / 1_048_576)
    if skipped:
        log.warning("%s: %d file(s) were locked — close its Chrome for a full backup",
                    profile, skipped)
    return {"profile": Path(profile).name, "object": name, "bytes": size,
            "files": added, "skipped": skipped}


def backup_all() -> dict:
    """Back up every profile directory under data/profiles/."""
    root = _profiles_dir()
    if not root.is_dir():
        log.warning("no profiles directory at %s", root)
        return {"backed_up": 0, "profiles": []}
    done = []
    for path in sorted(root.iterdir()):
        if path.is_dir():
            try:
                done.append(backup(path.name))
            except Exception as exc:  # noqa: BLE001 — one bad profile mustn't stop the rest
                log.error("backup of %s failed: %s", path.name, exc)
    log.info("session backup done — %d profile(s)", len(done))
    return {"backed_up": len(done), "profiles": done}


def list_sessions() -> list[dict]:
    """Backed-up sessions as [{name, size}]."""
    items = mc.list_objects(BUCKET)
    log.info("%d session backup(s) in MinIO", len(items))
    return items


def restore(profile: str, overwrite: bool = False) -> dict:
    """Download a profile's backup and extract it into data/profiles/<profile>.

    Refuses a non-empty target unless overwrite=True, so it can't wipe a live
    login by mistake."""
    name = _obj_name(profile)
    data = mc.get_bytes(BUCKET, name)
    if data is None:
        raise FileNotFoundError(f"no backup {name!r} in MinIO")

    dest = _profiles_dir() / Path(profile).name
    if dest.exists() and any(dest.iterdir()) and not overwrite:
        raise FileExistsError(
            f"{dest} already has a profile; pass overwrite=True to replace it"
        )
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)
    log.info("restored %s from %s into %s", profile, name, dest)
    return {"profile": Path(profile).name, "restored_to": str(dest)}


def _main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    if cmd == "status":
        mc.status()
        return 0
    if not mc.is_up():
        log.error("MinIO is stopped — start it, then try again")
        return 1
    if cmd == "backup":
        backup_all()
        return 0
    if cmd in ("list", "pull"):
        items = list_sessions()
        print(f"{len(items)} session backup(s) in MinIO:")
        for it in items:
            print(f"  - {it['name']}  ({it['size'] / 1_048_576:.1f} MB)")
        return 0
    if cmd == "restore" and len(argv) > 1:
        print(restore(argv[1], overwrite="--force" in argv))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
