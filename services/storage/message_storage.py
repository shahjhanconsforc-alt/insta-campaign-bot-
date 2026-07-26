"""Store the campaign's SMS message drafts in MinIO.

The drafts (id + name + text per draft) live as one tab-separated text object —
sms_drafts.txt — in the messages bucket, one draft per line. push() replaces,
load() reads back. A message body may well contain line breaks, so the text is
escaped by the shared tsv codec rather than written raw. (An older build stored
sms_drafts.json; it is read once and rewritten as text automatically.)

    python -m services.storage.message_storage push   # local drafts -> MinIO
    python -m services.storage.message_storage list
"""
from __future__ import annotations

import json

from . import minio_client as mc
from . import tsv

BUCKET = mc.BUCKET_MESSAGES
OBJECT_NAME = "sms_drafts.txt"
_LEGACY_JSON = "sms_drafts.json"      # older format, read once then migrated to .txt

_FIELDS = ("id", "name", "text")

log = mc.log


def push(drafts: list) -> dict:
    """Store the given drafts, replacing what's there. Returns the count."""
    rows = [d if isinstance(d, dict) else {"text": str(d or "")}
            for d in (drafts or [])]
    mc.put_bytes(BUCKET, OBJECT_NAME, tsv.dumps(rows, _FIELDS),
                 "text/tab-separated-values; charset=utf-8")
    log.info("pushed %d SMS draft(s) to MinIO", len(rows))
    return {"pushed": len(rows), "bucket": BUCKET, "object": OBJECT_NAME}


def _parse_legacy_json(raw: bytes) -> list:
    payload = json.loads(raw.decode("utf-8"))
    drafts = payload.get("drafts") if isinstance(payload, dict) else payload
    return drafts if isinstance(drafts, list) else []


def load() -> list:
    """The stored drafts, or an empty list if none saved yet."""
    raw = mc.get_bytes(BUCKET, OBJECT_NAME)
    if raw is None:
        legacy = mc.get_bytes(BUCKET, _LEGACY_JSON)
        if legacy is None:
            log.info("no SMS drafts stored yet")
            return []
        drafts = _parse_legacy_json(legacy)
        log.info("loaded %d SMS draft(s) from legacy %s", len(drafts), _LEGACY_JSON)
        return drafts
    if tsv.looks_like_json(raw):                      # .txt written by an older build
        drafts = _parse_legacy_json(raw)
    else:
        drafts = tsv.loads(raw, _FIELDS)
    log.info("loaded %d SMS draft(s) from MinIO", len(drafts))
    return drafts


def _main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    if cmd == "status":
        mc.status()
        return 0
    if not mc.is_up():
        log.error("MinIO is stopped — start it, then try again")
        return 1
    if cmd == "push":
        from backend import db

        drafts = db.get_sms_drafts()
        log.info("read %d draft(s) from local Data", len(drafts))
        push(drafts)
        return 0
    if cmd in ("list", "pull"):
        drafts = load()
        print(f"{len(drafts)} SMS draft(s) in MinIO:")
        for d in drafts:
            text = str(d.get("text", "") if isinstance(d, dict) else d)
            print(f"  - {text[:60]}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
