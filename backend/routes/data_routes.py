from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import db
from services.storage.minio_client import MinioUnavailable

router = APIRouter(prefix="/api/data", tags=["data"])


class RecordBody(BaseModel):
    title: str = "untitled"
    body: str = ""
    type: str = "note"


@router.get("/status")
async def status():
    return db.status()


@router.get("/records")
async def records(limit: int = 300):
    # limit=0 (or negative) means "everything" — the Data panel asks for that so
    # its name count reflects the whole store, not just the newest `limit` rows.
    return {"records": db.get_all(limit=limit), "count": db.count()}


@router.post("/records")
async def create(body: RecordBody):
    return {"record": db.insert(body.model_dump())}


@router.delete("/records/{record_id}")
async def delete(record_id: int):
    if not db.remove(record_id):
        raise HTTPException(404, f"record {record_id} not found")
    return {"deleted": record_id}


# ── Usernames: MinIO is their source of truth (not local records) ──
@router.get("/usernames")
async def usernames():
    """Every stored Instagram username, from MinIO. The Data panel and the
    campaign both load their name list from here."""
    try:
        names = db.list_usernames()
        return {"usernames": names, "count": len(names)}
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc


@router.delete("/usernames/{name}")
async def delete_username(name: str):
    """Remove one username from MinIO — the ✕ next to a name in the Data panel."""
    try:
        return db.remove_username(name)
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc


@router.get("/usernames-bio")
async def usernames_bio(exclude_categorized: bool = False):
    """Every stored username + bio record, from MinIO's usernames_bio.json — the
    richer data the profile-visiting scrape (Runs → 🔎 Open each profile) collects.

    `exclude_categorized=true` drops any handle that already has a category in
    usernames_categories.json. The usersBio view passes it so the two Data panel
    views partition the set rather than overlap: a handle shows under usersBio only
    while it is still uncategorized, and under "Categorized data" once it isn't.
    Returns `total` (everything stored) alongside `count` (what came back), so the
    panel can say how many were left out."""
    try:
        records = db.list_username_bios()
        total = len(records)
        if exclude_categorized:
            done = db.categorized_usernames()          # lower-cased handles
            records = [r for r in records
                       if str(r.get("username") or "").strip().lstrip("@").lower() not in done]
        return {"records": records, "count": len(records), "total": total}
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc


@router.get("/usernames-categories")
async def usernames_categories():
    """Every categorized handle from MinIO's usernames_categories.json — what the
    Data panel's "Categorized data" view and the Campaign wizard's Users step read.

    Each row carries its own `used_count` — the column moved here with the handle
    when categorized rows were purged out of the bio store, so no join is needed
    and the wizard can filter on the count and segment on the category in one pass.

    Also returns the category tally, so the tick list and the table are built from
    one response and can never disagree. The categories are whatever the data
    actually holds, not a fixed list — the closed taxonomy left with the pipeline
    that wrote it.
    """
    try:
        records = db.list_username_categories()
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc

    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("category") or "").strip()
        counts[key] = counts.get(key, 0) + 1

    categories = [{"name": name, "count": n}
                  for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"records": records, "count": len(records), "categories": categories}


@router.delete("/usernames-bio/{name}")
async def delete_username_bio(name: str):
    """Remove one handle from usernames_bio.json — the ✕ in the usersBio view. Also
    drops it from usernames.txt so the handle is gone from both name lists."""
    try:
        result = db.remove_username_bio(name)
        db.remove_username(name)                        # keep usernames.txt in step
        return result
    except MinioUnavailable as exc:
        raise HTTPException(503, f"MinIO is unavailable — {exc}") from exc


@router.post("/clear")
async def clear():
    db.clear()
    return {"cleared": True}
