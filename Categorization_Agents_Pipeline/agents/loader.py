"""Agent 1 — Data Loader.

Reads {username, bio} out of MinIO and lets through only what is worth
processing. Everything downstream may then assume a real handle and a bio with
something in it, which is why no other agent carries emptiness checks.

Skipping is counted, not silent: a run reports how many rows it declined and
why, because "2000 profiles produced 40 verdicts" needs an explanation and
"they had no bio" is one.
"""
from __future__ import annotations

import re

from .. import config
from ..contracts import Profile

# Not bios — Instagram's own chrome, caught by the profile scraper when a page
# hadn't finished rendering. ~15% of the stored rows are one of these, and
# without this filter they land as "processed but uncategorizable" and make the
# hit rate look like a pipeline problem when it is a scrape problem.
#
# Kept deliberately narrow: only strings that CANNOT be a real bio. A one-word
# bio like "photographer" is real signal, so there is no "looks like a handle"
# rule here — that would throw away genuine single-word bios to catch noise the
# lexicon simply fails to match anyway.
_ARTIFACTS = (
    re.compile(r"^\d[\d,.\s]*posts?$", re.I),               # "444 posts"
    re.compile(r"^\d[\d,.\s]*(followers?|following)$", re.I),
    re.compile(r"^messages$", re.I),                        # the DM button's label
    re.compile(r"^follow(ing|ed|s)?$", re.I),
    re.compile(r"you'?ve been on instagram", re.I),         # the usage nag
    re.compile(r"^(edit profile|view profile|profile)$", re.I),
)


class DataLoaderAgent:
    name = "loader"

    def __init__(self, source: str = "bio"):
        # "bio" = usernames_bio.txt (the not-yet-categorized set, the normal job).
        # "categories" re-runs over already-categorized handles, for when the
        # lexicon has improved and you want the old verdicts refreshed.
        self.source = source
        self.skipped: dict[str, int] = {}

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def load(self, limit: int | None = None) -> list[Profile]:
        """Every processable profile from the store, newest handling first."""
        from backend import db

        rows = (db.list_username_categories() if self.source == "categories"
                else db.list_username_bios())
        return self.validate(rows, limit=limit)

    def validate(self, rows, limit: int | None = None) -> list[Profile]:
        """Filter raw store rows down to processable profiles.

        Split out from `load` so the agent can be tested — and re-used over any
        row source — without a MinIO.
        """
        out: list[Profile] = []
        seen: set[str] = set()
        for row in rows or []:
            if not isinstance(row, dict):
                self._skip("not a record")
                continue
            name = str(row.get("username") or "").strip().lstrip("@")
            if not name:
                self._skip("no username")
                continue
            key = name.lower()
            if key in seen:                      # the store dedups, but never trust it
                self._skip("duplicate")
                continue
            bio = str(row.get("bio") or "").strip()
            if bio.lower() == config.UNKNOWN_BIO:
                # The store's "collected the name, never opened the profile"
                # sentinel — there is genuinely nothing to read here.
                self._skip("no bio scraped")
                continue
            if len(bio) < config.MIN_BIO_CHARS:
                self._skip("bio too short")
                continue
            if any(p.match(bio) or p.search(bio) for p in _ARTIFACTS):
                self._skip("not a bio (scraper artifact)")
                continue
            seen.add(key)
            out.append(Profile(username=name, bio=bio))
            if limit and len(out) >= limit:
                break
        return out

    def report(self) -> dict:
        return {"agent": self.name, "skipped": dict(self.skipped),
                "skipped_total": sum(self.skipped.values())}
