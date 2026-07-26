"""Local cache dir for transient files pulled from MinIO.

Some things physically need a real file on disk at the moment they're used — a
media image being attached to a DM, a session profile Chrome launches from. MinIO
stays the source of truth; this is just the scratch space those get materialised
into. Everything here is disposable: safe to delete, re-fetched on next use.
"""
from __future__ import annotations

from pathlib import Path

from backend import config

# Under backend/data so it sits with the other runtime dirs and is covered by
# the same .gitignore. Named 'cache' to make its throwaway nature obvious.
CACHE_DIR = config.BACKEND_DIR / "data" / "minio-cache"


def cache_dir(*parts: str) -> Path:
    """A subdirectory of the cache (created if missing), e.g. cache_dir('media')."""
    path = CACHE_DIR.joinpath(*parts) if parts else CACHE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(name: str, *, sub: str = "") -> Path:
    """A path inside the cache for `name` (any directory part of name stripped)."""
    return cache_dir(sub) / Path(str(name)).name
