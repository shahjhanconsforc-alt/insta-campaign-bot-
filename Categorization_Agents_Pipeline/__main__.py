"""Run the pipeline from the command line.

    python -m Categorization_Agents_Pipeline --dry-run --limit 50
    python -m Categorization_Agents_Pipeline --workers 12
    python -m Categorization_Agents_Pipeline --explain hunza_traveller

--dry-run is the default-safe way to look first: it runs every agent and prints
what WOULD be saved without writing to MinIO.
"""
from __future__ import annotations

import argparse
import json
import sys


def _explain(username: str) -> int:
    """Show the full agent chain for one handle — the debugging path when a
    verdict looks wrong and you need to see which signal caused it."""
    from backend import db
    from .contracts import Profile
    from .pipeline import CategorizationPipeline

    row = next((r for r in db.list_username_bios() + db.list_username_categories()
                if str(r.get("username", "")).lower() == username.lower()), None)
    if row is None:
        print(f"@{username} is not in either store.")
        return 1

    pipe = CategorizationPipeline(dry_run=True)
    profile = Profile(username=str(row["username"]), bio=str(row.get("bio") or ""))
    clean = pipe.cleaner.run(profile)
    signals = pipe.interests.run(clean) + pipe.entities.run(clean)
    scored = pipe.confidence.score(signals)
    kept = pipe.confidence.keep(scored)
    verdict = pipe.process(profile)

    print(f"@{profile.username}")
    print(f"  bio       : {profile.bio!r}")
    print(f"  cleaned   : {clean.text!r}")
    print(f"  language  : {clean.language}")
    print(f"  emoji     : {''.join(clean.emojis) or '—'}")
    print(f"  hashtags  : {clean.hashtags or '—'}")
    print("  signals   :")
    for s in signals:
        print(f"      {s.kind:<8} {s.subject:<28} <- {s.evidence!r}"
              + (f"  [{s.facet}]" if s.facet else ""))
    print("  scored    :")
    for k, v in sorted(scored.items(), key=lambda kv: -kv[1]):
        mark = "keep" if k in kept else "drop"
        print(f"      {v:>5.3f}  {mark}  {k}")
    print(f"  categories: {verdict.categories}")
    print(f"  tags      : {verdict.tags}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="Categorization_Agents_Pipeline")
    ap.add_argument("--workers", type=int, default=None, help="concurrent workers")
    ap.add_argument("--limit", type=int, default=None, help="cap profiles processed")
    ap.add_argument("--dry-run", action="store_true", help="run without writing")
    ap.add_argument("--source", choices=("bio", "categories"), default="bio",
                    help="bio = uncategorized handles (default); categories = re-run")
    ap.add_argument("--sample", type=int, default=0, help="print N verdicts")
    ap.add_argument("--explain", metavar="USERNAME", help="trace one handle and exit")
    args = ap.parse_args(argv)

    if args.explain:
        return _explain(args.explain)

    from .pipeline import CategorizationPipeline

    def tick(done, total):
        print(f"  … {done}/{total}", flush=True)

    pipe = CategorizationPipeline(workers=args.workers, dry_run=args.dry_run,
                                  source=args.source, progress=tick)
    summary = pipe.run(limit=args.limit)

    if args.sample:
        print("\n--- sample verdicts ---")
        for row in pipe.writer.preview[:args.sample]:
            print(json.dumps({"username": row["username"],
                              "categories": row["categories"],
                              "tags": row["tags"]}, ensure_ascii=False))
    print("\n--- summary ---")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
