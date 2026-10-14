"""Read the categorized Instagram handles out of MinIO.

Parallel to insta_bio_storage (which keeps the scrapes' not-yet-categorized rows),
this holds usernames_categories.txt in the usernames bucket: a tab-separated table,
one record per line, deduped by username, each carrying the top-level `category` a
handle was sorted into plus whatever precise `specifics` / `locations` were worked
out for it. (An older build stored usernames_categories.json; it is read once and
rewritten as text automatically — see load_all.)

Written by Categorization_Agents_Pipeline, which is the only producer of a
category. Everything else here treats the table as read-only; there is
deliberately no clear(), because this object holds work that a re-run costs real
time to reproduce.

`used_count` is the exception to "the pipeline owns this row": campaign usage is
tracked per handle, and once a handle lives here rather than in the bio store this
is the only place that count can go. bump_uses() updates that column and nothing
else, and upsert() preserves whatever count a row already had.

    load_all()      read the table back
    upsert(rows)    add/refresh categorized rows (the pipeline's writer)
    categorized()   the set of handles already in here
    bump_uses()     +1 to used_count for handles stored here
    count()         how many are stored

Every operation logs to the terminal via the minio logger.
"""
from __future__ import annotations

import json
import threading

from . import minio_client as mc
from . import tsv

OBJECT_NAME = "usernames_categories.txt"
_LEGACY_JSON = "usernames_categories.json"   # older format, read once then migrated

# Read-modify-write for bump_uses has to be atomic within this process, the same
# guard the other stores use around their object.
_lock = threading.Lock()

log = mc.log

# The stored columns. `used_count` is carried here (rather than left in the bio
# store) because a categorized handle no longer has a row over there to hold it.
USED_FIELD = "used_count"
# Last campaign outcome on the handle ("sent" / "failed" / "") and why it failed —
# campaign facts like used_count, never reset by a re-categorization.
STATUS_FIELD = "campaign_status"
REASON_FIELD = "fail_reason"
# `category` stays a single string — the Data panel's tick list and the campaign
# wizard both key off one category per handle, so the pipeline's best category
# goes there and the FULL set rides alongside in `categories`. That is what lets
# a multi-category pipeline write into the table the UI already reads without the
# UI changing at all.
_FIELDS = ("username", "bio", "category", "categories", "tags",
           "specifics", "locations", USED_FIELD, STATUS_FIELD, REASON_FIELD)

# The list-valued columns; each rides in one column, semicolon-joined. A
# semicolon is safe here in a way a comma is not: these hold short interest,
# place and tag names, and the scraped set contains no semicolons at all.
_LIST_FIELDS = ("categories", "tags", "specifics", "locations")
_LIST_SEP = "; "

# The category a record carries when it was read but nothing fitted, and when the
# bio held no signal at all. Kept literal so the panel can tell the two
# non-answers apart from a real bucket.
OTHER = "Other"
UNKNOWN = "Unknown"


def _as_list(value) -> list[str]:
    """A stored list column back into a list of clean strings."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split(";") if p.strip()]


def _row_out(record: dict) -> dict:
    """One record on its way to disk: lists flattened into their column."""
    out = dict(record)
    for field in _LIST_FIELDS:
        out[field] = _LIST_SEP.join(_as_list(record.get(field)))
    try:
        out[USED_FIELD] = max(0, int(record.get(USED_FIELD) or 0))
    except (TypeError, ValueError):
        out[USED_FIELD] = 0
    return out


def _row_in(record: dict) -> dict:
    """One record on its way back, with the list columns rebuilt."""
    out = dict(record)
    out["username"] = str(record.get("username") or "").strip().lstrip("@")
    for field in _LIST_FIELDS:
        out[field] = _as_list(record.get(field))
    try:
        out[USED_FIELD] = max(0, int(record.get(USED_FIELD) or 0))
    except (TypeError, ValueError):
        out[USED_FIELD] = 0
    return out


def _parse_legacy_json(raw: bytes) -> list[dict]:
    """The old usernames_categories.json body, read so it can become text."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("%s is not valid JSON — treating as empty", _LEGACY_JSON)
        return []
    if isinstance(data, dict):                       # tolerate a wrapped payload
        data = data.get("records") or data.get("profiles") or []
    rows = [r for r in (data if isinstance(data, list) else []) if isinstance(r, dict)]
    return [_row_in(r) for r in rows]


def load_all() -> list[dict]:
    """Every stored categorized record. Empty list if nothing's there yet.

    Reads usernames_categories.txt; falls back to the legacy JSON object when the
    text one isn't there yet, so nothing is lost before the migration runs.
    Raises MinioUnavailable if the server is down, rather than returning [] — an
    empty list would read as "nothing is categorized" and quietly blank the panel.
    """
    raw = mc.get_bytes(mc.BUCKET, OBJECT_NAME)
    if raw is None:
        legacy = mc.get_bytes(mc.BUCKET, _LEGACY_JSON)
        if legacy is None:
            return []
        log.info("%s not found — reading legacy %s", OBJECT_NAME, _LEGACY_JSON)
        return _parse_legacy_json(legacy)
    if tsv.looks_like_json(raw):                     # .txt written by an older build
        return _parse_legacy_json(raw)
    return [_row_in(r) for r in tsv.loads(raw, _FIELDS)]


def _put(records: list[dict]) -> int:
    """Write the whole table as usernames_categories.txt. Overwrites."""
    body = tsv.dumps([_row_out(r) for r in records], _FIELDS)
    mc.put_bytes(mc.BUCKET, OBJECT_NAME, body,
                 content_type="text/tab-separated-values; charset=utf-8")
    return len(records)


def upsert(records) -> dict:
    """Merge categorized rows in, keyed by username (case-insensitive).

    A handle already present is REFRESHED — the pipeline is deterministic, so a
    re-run after the lexicon improved should replace the old verdict rather than
    sit beside it. The one field never taken from the incoming row is
    `used_count`: campaign usage belongs to the handle, not to the verdict, and
    a re-categorization must not reset it to zero.

    Returns {"added": n, "updated": n, "total": n}.
    """
    incoming = [_row_in(r) for r in (records or [])
                if isinstance(r, dict) and str(r.get("username") or "").strip()]
    if not incoming:
        return {"added": 0, "updated": 0, "total": count()}
    with _lock:
        rows = load_all()                            # raises MinioUnavailable if down
        by_key = {r["username"].lower(): r for r in rows if r.get("username")}
        added = updated = 0
        for rec in incoming:
            key = rec["username"].lower()
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = rec
                added += 1
            else:
                rec[USED_FIELD] = int(existing.get(USED_FIELD) or 0)   # usage survives
                rec[STATUS_FIELD] = existing.get(STATUS_FIELD) or ""   # and so does the outcome
                rec[REASON_FIELD] = existing.get(REASON_FIELD) or ""
                by_key[key] = rec
                updated += 1
        merged = list(by_key.values())
        _put(merged)
        log.info("category store: +%d new, %d refreshed (total %d)",
                 added, updated, len(merged))
        return {"added": added, "updated": updated, "total": len(merged)}


def categorized() -> set[str]:
    """Lower-cased usernames that have a category — what the Data panel's usersBio
    view subtracts, so a handle shows in exactly one of the two views."""
    return {
        name.lower()
        for name in (
            str(r.get("username") or "").strip().lstrip("@") for r in load_all()
        )
        if name
    }


def bump_uses(names) -> dict:
    """+1 to `used_count` for each stored handle in `names` (once per occurrence).

    Only handles ALREADY in this table are touched — a campaign that reaches an
    uncategorized handle is counted by the bio store instead, and inventing a
    categorized row here would fabricate a category nobody assigned. Returns
    {"bumped", "missing"}: `missing` is the handles not stored here, which the
    caller passes on to the bio store.
    """
    wanted: list[str] = [str(n or "").strip().lstrip("@") for n in (names or [])]
    wanted = [n for n in wanted if n]
    if not wanted:
        return {"bumped": 0, "missing": []}
    with _lock:
        rows = load_all()                            # raises MinioUnavailable if down
        by_key = {r["username"].lower(): r for r in rows if r.get("username")}
        bumped, missing = 0, []
        for name in wanted:
            row = by_key.get(name.lower())
            if row is None:
                missing.append(name)
            else:
                row[USED_FIELD] = int(row.get(USED_FIELD) or 0) + 1
                bumped += 1
        if bumped:
            _put(rows)
        log.info("category store: bumped used_count on %d handle(s), %d not here",
                 bumped, len(missing))
        return {"bumped": bumped, "missing": missing}


def set_statuses(mapping) -> dict:
    """Record each stored handle's last campaign outcome: `mapping` is
    {username: (status, reason)}, status "sent" or "failed". Only handles already
    here are touched; the rest come back in `missing` for the bio store."""
    wanted = {}
    for key, value in (mapping or {}).items():
        name = str(key or "").strip().lstrip("@")
        status, reason = (value if isinstance(value, (tuple, list)) else (value, ""))[:2]
        status = str(status or "").strip().lower()
        if name and status in ("sent", "failed"):
            wanted[name] = (status, str(reason or "")[:160] if status == "failed" else "")
    if not wanted:
        return {"updated": 0, "missing": []}
    with _lock:
        rows = load_all()
        by_key = {r["username"].lower(): r for r in rows if r.get("username")}
        updated, missing = 0, []
        for name, (status, reason) in wanted.items():
            row = by_key.get(name.lower())
            if row is None:
                missing.append(name)
            else:
                row[STATUS_FIELD] = status
                row[REASON_FIELD] = reason
                updated += 1
        if updated:
            _put(rows)
        log.info("category store: campaign status on %d handle(s), %d not here",
                 updated, len(missing))
        return {"updated": updated, "missing": missing}


def adopt_uses(mapping) -> dict:
    """Copy `{username: used_count}` onto the rows stored here, keeping the higher
    of the two counts.

    Used once, when categorized handles are purged out of the bio store: their
    campaign usage would otherwise go with them. Idempotent — re-running it can
    only ever leave the counts where they are. Returns {"updated", "total"}.
    """
    norm = {}
    for key, value in (mapping or {}).items():
        name = str(key or "").strip().lstrip("@").lower()
        try:
            count = max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
        if name and count:
            norm[name] = count
    with _lock:
        rows = load_all()
        updated = 0
        for row in rows:
            want = norm.get(str(row.get("username") or "").lower())
            if want and want > int(row.get(USED_FIELD) or 0):
                row[USED_FIELD] = want
                updated += 1
        if updated:
            _put(rows)
        log.info("category store: adopted used_count on %d row(s) (total %d)",
                 updated, len(rows))
        return {"updated": updated, "total": len(rows)}


def count() -> int:
    """How many categorized records are stored."""
    return len(load_all())
