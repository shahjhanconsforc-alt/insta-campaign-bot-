"""Ingest verdicts decided by a reasoning pass, not by the lexicon.

The deterministic agents only know what lexicon.py knows. A bio written in Urdu,
or one whose meaning is idiomatic rather than keyworded ("chasing sunsets since
'98"), reads as no-signal to them. Those handles are worked out by reading them,
and the results come back through here.

This is NOT a second pipeline: verdicts land in the same table, in the same
schema, through the same WriterAgent, so a row categorized this way is
indistinguishable downstream from one the lexicon produced. What differs is only
who decided it — recorded in `method` so a later run can tell them apart.

    python -m Categorization_Agents_Pipeline.ingest verdicts.jsonl

Each line is one verdict:

    {"username": "x", "categories": ["Travel"], "tags": ["Travel", "loc:Hunza"]}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .contracts import Verdict


def parse(path: str | Path) -> list[Verdict]:
    """Read a JSONL verdict file into Verdicts, skipping malformed lines."""
    out: list[Verdict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(row.get("username") or "").strip().lstrip("@")
        if not name:
            continue
        cats = [str(c).strip() for c in (row.get("categories") or []) if str(c).strip()]
        tags = [str(t).strip() for t in (row.get("tags") or []) if str(t).strip()]
        # The location rule holds however a verdict was reached.
        if any(t.startswith("loc:") for t in tags) and "Travel" not in tags:
            tags.insert(0, "Travel")
        if "Travel" in tags and "Travel" not in cats:
            cats.insert(0, "Travel")
        out.append(Verdict(username=name, bio=str(row.get("bio") or ""),
                           categories=cats, tags=tags))
    return out


def ingest(path: str | Path, dry_run: bool = False) -> dict:
    """Write a verdict file into the categorized table.

    The stored `bio` is taken from the store, never from the file — the verdict
    decides categories, not content, and re-writing the bio from a hand-made file
    is how a transcription slip would silently corrupt the table.
    """
    from backend import db
    from .agents import WriterAgent

    verdicts = parse(path)
    known = {str(r.get("username", "")).lower(): str(r.get("bio") or "")
             for r in db.list_username_bios() + db.list_username_categories()}

    writer = WriterAgent(dry_run=dry_run)
    skipped = 0
    for v in verdicts:
        bio = known.get(v.username.lower())
        if bio is None:
            skipped += 1                      # not a handle we store — ignore it
            continue
        v.bio = bio
        writer.submit(v)
    writer.flush()
    return {"parsed": len(verdicts), "written": writer.written,
            "unknown_handles": skipped, "errors": writer.errors}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    print(json.dumps(ingest(args[0], dry_run="--dry-run" in sys.argv), indent=2))
