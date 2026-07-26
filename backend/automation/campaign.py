"""Campaign planning helper.

Builds a manual review queue from saved Instagram-name records.
This module never opens Instagram or sends messages.
"""
from __future__ import annotations

from backend import activity, db

DEFAULT_LIMIT = 25
SCAN_LIMIT = 1000


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _collect_targets(limit: int) -> list[dict]:
    # Usernames come from MinIO now (their source of truth), not local records.
    seen: set[str] = set()
    targets: list[dict] = []

    for raw in db.list_usernames():
        name = _clean_text(raw)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        targets.append({"record_id": None, "name": name,
                        "created_at": None, "note": ""})

    return targets[: max(1, limit)]


def build_plan(message: str, limit: int = DEFAULT_LIMIT, save_draft: bool = True) -> dict:
    """Prepare a manual outreach queue from saved Data records."""
    msg = _clean_text(message)
    if not msg:
        raise ValueError("Campaign message is required.")

    try:
        limit = int(limit)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Campaign limit must be a number.") from exc
    limit = max(1, min(limit, 200))

    targets = _collect_targets(limit)
    if not targets:
        raise RuntimeError("No saved Instagram names in Data. Save some names first.")

    summary = (
        f"Prepared {len(targets)} manual review target(s) from saved Data records. "
        "Review the list before sending anything."
    )
    # No longer persists a "campaign-draft" record — the Data panel is for
    # usernames, not a log of every prepared queue. `save_draft` is kept in the
    # signature for compatibility but is a no-op now.
    activity.log("campaign", f"Prepared review queue with {len(targets)} target(s)", "ok")

    return {
        "message": msg,
        "limit": limit,
        "count": len(targets),
        "targets": targets,
        "first_target": targets[0],
        "summary": summary,
        "draft": None,
    }
