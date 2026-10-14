"""Tiny JSON-file database.

Loaded once into memory, written through to disk on every mutation. Fine for a
single-process agent; swap for SQLite if you ever need concurrent writers.
"""
import json
import threading
from datetime import datetime, timezone
from typing import Any

from backend import config

_LOCK = threading.Lock()
_EMPTY: dict[str, Any] = {"records": [], "runs": [], "accounts": [], "settings": {},
                         "sms_drafts": [], "seq": 0}
_cache: dict[str, Any] | None = None

MAX_RUNS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with config.DB_FILE.open("r", encoding="utf-8") as handle:
            _cache = {**_EMPTY, **json.load(handle)}
    except (FileNotFoundError, json.JSONDecodeError):
        _cache = {"records": [], "runs": [], "accounts": [], "seq": 0}
        _save()
    return _cache


def _save() -> None:
    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.DB_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(_cache, handle, indent=2, default=str)
    tmp.replace(config.DB_FILE)  # atomic — never leaves a half-written store


def insert(data: dict) -> dict:
    with _LOCK:
        store = _load()
        store["seq"] += 1
        record = {"id": store["seq"], "created_at": _now(), **data}
        store["records"].append(record)
        _save()
        return record


def add_unique_names(names: list[str], record_type: str = "instagram-name") -> dict:
    """Store usernames in MinIO (the source of truth), skipping duplicates.

    Returns {"added": [names], "skipped": [names]} — the actual new and
    already-present names, case-insensitive. `record_type` is kept for signature
    compatibility; only instagram-name usernames are stored this way.
    """
    from services.storage import insta_username_storage as us

    result = us.add(names)
    return {"added": result["added"], "skipped": result["skipped"]}


def record_scraped_usernames(names: list[str]) -> dict:
    """Persist a scrape's collected usernames to BOTH stores the UI reads.

    Every scrape from the Scrapping panel lands its names here: usernames.txt
    (the plain list / campaign name set) AND usernames_bio.txt as
    {username, bio}. A name collected without a bio is stored with bio
    "unknown"; a real bio already on the row is never downgraded (so a fast
    name-only pass can't wipe a bio an earlier profile visit read). Returns the
    usernames.txt outcome {"added", "skipped"} — the counts the scrape routes
    report.

    A scrape has no idea which handles are already categorized, so it will happily
    re-add one; the purge afterwards puts the split back, keeping usernames_bio.txt
    to the not-yet-categorized set.
    """
    outcome = add_unique_names(names)
    try:
        add_username_bios([{"username": n} for n in names])
    except Exception as exc:  # noqa: BLE001 — the bio row is secondary; names are saved
        from backend import activity

        activity.log("data",
                     f"usernames_bio.txt write skipped for {len(names)} name(s): {exc}",
                     "warn")
    try:
        purge_categorized_bios()
    except Exception as exc:  # noqa: BLE001 — a failed purge just leaves an overlap
        from backend import activity

        activity.log("data", f"Categorized-handle purge skipped: {exc}", "warn")
    return outcome


def list_usernames() -> list[str]:
    """Every stored username, from MinIO."""
    from services.storage import insta_username_storage as us

    return us.load_all()


def remove_username(name: str) -> dict:
    """Delete one username from MinIO."""
    from services.storage import insta_username_storage as us

    return us.remove(name)


def add_username_bios(records: list[dict]) -> dict:
    """Store username + bio (and the rest of a profile's data) in MinIO's
    usernames_bio.json, upserting by username. Returns {"added", "updated",
    "total"}. Separate from add_unique_names, which only keeps the bare names."""
    from services.storage import insta_bio_storage as bios

    return bios.add(records)


def list_username_bios() -> list[dict]:
    """Every stored username + bio record, from MinIO's usernames_bio.json."""
    from services.storage import insta_bio_storage as bios

    return bios.load_all()


def remove_username_bio(name: str) -> dict:
    """Delete one username's row from MinIO's usernames_bio.json. Returns
    {"removed", "total"}."""
    from services.storage import insta_bio_storage as bios

    return bios.remove(name)


def bump_username_uses(names: list[str]) -> dict:
    """+1 to each name's `used_count` — called when a campaign stages/sends to them.

    A handle lives in exactly one of the two stores, so the count has to follow it:
    the category store is asked first and reports back whichever names it doesn't
    hold, and only those go to the bio store (which adds a row for a name it has
    never seen). Returns {"bumped", "added", "total"}.
    """
    from services.storage import insta_bio_storage as bios
    from services.storage import insta_category_storage as cats

    try:
        first = cats.bump_uses(names)
    except Exception:  # noqa: BLE001 — a category-store hiccup mustn't lose the count
        first = {"bumped": 0, "missing": list(names or [])}
    rest = bios.bump_uses(first.get("missing") or [])
    return {
        "bumped": first.get("bumped", 0) + rest.get("bumped", 0),
        "added": rest.get("added", 0),
        "total": rest.get("total", 0),
    }


def record_campaign_outcomes(sent: list[str], failed: dict[str, str]) -> dict:
    """Stamp each handle with how its last campaign attempt ended — "sent", or
    "failed" with the reason — so the wizard can label and filter failed handles.
    Same routing as bump_username_uses: category store first, the rest to the
    bio store. Returns {"updated", "added"}."""
    from services.storage import insta_bio_storage as bios
    from services.storage import insta_category_storage as cats

    mapping: dict[str, tuple[str, str]] = {}
    for name in sent or []:
        mapping[str(name)] = ("sent", "")
    for name, reason in (failed or {}).items():
        mapping[str(name)] = ("failed", str(reason or "")[:160])
    if not mapping:
        return {"updated": 0, "added": 0}
    try:
        first = cats.set_statuses(mapping)
    except Exception:  # noqa: BLE001 — a category-store hiccup mustn't lose the outcome
        first = {"updated": 0, "missing": list(mapping)}
    rest = bios.set_statuses({n: mapping[n] for n in first.get("missing") or [] if n in mapping})
    return {"updated": first.get("updated", 0) + rest.get("updated", 0),
            "added": rest.get("added", 0)}


def purge_categorized_bios() -> dict:
    """Drop from usernames_bio.txt every handle that now has a category, after
    carrying its campaign usage across so the count isn't lost with the row.

    Called after a scrape, which saves every name it finds — including ones already
    categorized — so the two stores go back to partitioning the handles rather than
    overlapping. Returns {"removed", "total", "uses_moved"}.
    """
    from services.storage import insta_bio_storage as bios
    from services.storage import insta_category_storage as cats

    done = cats.categorized()
    uses = {}
    for row in bios.load_all():
        name = str(row.get("username") or "").strip().lstrip("@")
        count = int(row.get(bios.USED_FIELD) or 0)
        if name and count and name.lower() in done:
            uses[name] = count
    moved = cats.adopt_uses(uses).get("updated", 0) if uses else 0
    result = bios.purge_categorized()
    return {**result, "uses_moved": moved}


def list_username_categories() -> list[dict]:
    """Every categorized record, from MinIO's usernames_categories.json. Read-only
    — nothing writes this object now the AI pipeline is gone."""
    from services.storage import insta_category_storage as cats

    return cats.load_all()


def categorized_usernames() -> set[str]:
    """Lower-cased handles that carry a category, so the usersBio view can leave
    them out and each handle appears in exactly one Data panel view."""
    from services.storage import insta_category_storage as cats

    return cats.categorized()


def get_all(limit: int = 100) -> list[dict]:
    """Newest-first records. `limit <= 0` (or None) returns every record — the
    Data panel needs the true total, not a capped window that pins the visible
    name count at the limit no matter how many names are really stored."""
    records = _load()["records"]
    if not limit or limit <= 0:
        return list(reversed(records))
    return list(reversed(records[-limit:]))


def get_by_id(record_id: int) -> dict | None:
    return next((r for r in _load()["records"] if r["id"] == int(record_id)), None)


def remove(record_id: int) -> bool:
    with _LOCK:
        store = _load()
        before = len(store["records"])
        store["records"] = [r for r in store["records"] if r["id"] != int(record_id)]
        changed = len(store["records"]) != before
        if changed:
            _save()
        return changed


def clear() -> bool:
    """Wipes records and runs. Accounts are deliberately left alone — they hold
    credentials the user typed in, and "Clear all" on the Data panel must not
    silently destroy them."""
    with _LOCK:
        store = _load()
        store["records"] = []
        store["runs"] = []
        _save()
        return True


# ─── Settings ────────────────────────────────────────────────────────────


def get_settings() -> dict:
    return dict(_load().get("settings") or {})


def save_settings(values: dict) -> dict:
    with _LOCK:
        store = _load()
        store["settings"] = dict(values)
        _save()
        return dict(values)


# ─── Scrapping panel prefs ───────────────────────────────────────
# The Scrapping panel's own selections that aren't global settings: the 🎯 target
# count and each account's chosen technique. Kept in their own top-level key so
# they persist to the file (survive restart/refresh) without riding the settings
# blob, which settings.update() rewrites wholesale.

def get_scrape_prefs() -> dict:
    p = dict(_load().get("scrape_prefs") or {})
    p.setdefault("target", 0)          # 0 = unlimited: scrape until the feed dries up
    p.setdefault("techniques", {})
    return p


def save_scrape_prefs(changes: dict) -> dict:
    """Merge a partial update into the stored scrape prefs and persist it."""
    with _LOCK:
        store = _load()
        cur = dict(store.get("scrape_prefs") or {})
        if "target" in changes and changes["target"] is not None:
            try:
                # 0 = unlimited, and no upper bound: a cap here silently truncated
                # long scrapes, which is exactly what the target is meant to leave
                # to the operator (or to the feed drying up).
                cur["target"] = max(0, int(changes["target"]))
            except (TypeError, ValueError):
                pass
        if isinstance(changes.get("techniques"), dict):
            techs = dict(cur.get("techniques") or {})
            for key, val in changes["techniques"].items():
                if val in ("scroll", "search", "both"):
                    techs[str(key)] = val
                elif val is None:
                    techs.pop(str(key), None)
            cur["techniques"] = techs
        cur.setdefault("target", 0)
        cur.setdefault("techniques", {})
        store["scrape_prefs"] = cur
        _save()
        return dict(cur)


# ─── Campaign SMS drafts ─────────────────────────────────────────
# The message drafts behind the campaign's sms1/sms2/… tabs. Kept in their own
# top-level key (NOT under "settings", which settings.update() rewrites wholesale
# and would drop anything it doesn't know about).


# SMS drafts live in MinIO now — the messages bucket is their source of truth.
# Kept behind these two functions so every existing caller is unchanged; a local
# in-memory copy answers reads when MinIO is briefly unreachable, and any drafts
# still sitting in an old store.json are migrated up on first read.
_sms_cache: list | None = None


def get_sms_drafts() -> list:
    global _sms_cache
    from services.storage import message_storage

    try:
        drafts = message_storage.load()
    except message_storage.mc.MinioUnavailable:
        if _sms_cache is not None:
            return list(_sms_cache)          # serve the last known copy
        raise
    # One-time migration: if MinIO has none but a legacy store.json still does,
    # push those up so nothing is lost on the switch to MinIO.
    if not drafts:
        legacy = _load().get("sms_drafts") or []
        if legacy:
            message_storage.push(legacy)
            drafts = list(legacy)
    _sms_cache = list(drafts)
    return list(drafts)


def save_sms_drafts(drafts: list) -> list:
    global _sms_cache
    from services.storage import message_storage

    message_storage.push(list(drafts))       # MinIO is the source of truth
    _sms_cache = list(drafts)
    # Drop any legacy copy from the local store so it can't shadow MinIO later.
    with _LOCK:
        store = _load()
        if store.get("sms_drafts"):
            store["sms_drafts"] = []
            _save()
    return list(drafts)


# ─── Accounts ────────────────────────────────────────────────────────────
# NOTE: passwords are stored in plaintext in this JSON file. It is gitignored
# and local-only. See README — this is a local convenience store, not a vault.


def add_account(data: dict) -> dict:
    with _LOCK:
        store = _load()
        store["seq"] += 1
        account = {
            "id": store["seq"],
            "created_at": _now(),
            "label": (data.get("label") or "").strip(),
            "login": (data.get("login") or "").strip(),
            "password": data.get("password") or "",
            "logged_in": False,
            "last_login": None,
            "last_result": None,
        }
        store["accounts"].append(account)
        _save()
        return account


def list_accounts() -> list[dict]:
    return list(_load()["accounts"])


def get_account(account_id: int) -> dict | None:
    return next((a for a in _load()["accounts"] if a["id"] == int(account_id)), None)


def update_account(account_id: int, changes: dict) -> dict | None:
    with _LOCK:
        store = _load()
        for account in store["accounts"]:
            if account["id"] == int(account_id):
                account.update(changes)
                _save()
                return account
        return None


def remove_account(account_id: int) -> bool:
    with _LOCK:
        store = _load()
        before = len(store["accounts"])
        store["accounts"] = [a for a in store["accounts"] if a["id"] != int(account_id)]
        changed = len(store["accounts"]) != before
        if changed:
            _save()
        return changed


def save_run(run: dict) -> dict:
    with _LOCK:
        store = _load()
        store["seq"] += 1
        entry = {"id": store["seq"], "created_at": _now(), **run}
        store["runs"].append(entry)
        store["runs"] = store["runs"][-MAX_RUNS:]
        _save()
        return entry


def get_runs(limit: int = 25) -> list[dict]:
    return list(reversed(_load()["runs"][-limit:]))


def count() -> int:
    return len(_load()["records"])


def status() -> dict:
    store = _load()
    return {
        "driver": "json-file",
        "file": str(config.DB_FILE.relative_to(config.ROOT_DIR)),
        "records": len(store["records"]),
        "runs": len(store["runs"]),
        "size_bytes": config.DB_FILE.stat().st_size if config.DB_FILE.exists() else 0,
    }
