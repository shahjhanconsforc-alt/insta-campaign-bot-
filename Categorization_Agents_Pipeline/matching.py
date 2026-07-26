"""Word-boundary matching over the lexicon.

Every agent that looks for known terms comes through here, for two reasons.

Correctness: matching must respect word boundaries, or "art" fires inside
"party", "vet" inside "velvet" and "ai" inside "hair" — the three false
positives that would otherwise dominate the results. `\\b` alone is not enough
for terms containing punctuation or spaces, so the pattern is built per term.

Speed: the lexicon holds a few thousand terms and the pipeline runs them against
thousands of bios. Testing terms one at a time is O(terms x bios); one compiled
alternation per group makes it a single pass, and it is built once at import.
Longest-first ordering matters — without it "real" would match before
"real estate" and the phrase would never be seen.
"""
from __future__ import annotations

import re
from functools import lru_cache


def _pattern_for(term: str) -> str:
    """One term as a boundary-safe pattern.

    Spaces are allowed to match any run of whitespace so a bio that breaks
    "real estate" across a line still matches, and a term that begins or ends
    with a non-word character can't use \\b there (it would never match).
    """
    parts = [re.escape(p) for p in term.split()]
    body = r"\s+".join(parts)
    left = r"\b" if term[:1].isalnum() else ""
    right = r"\b" if term[-1:].isalnum() else ""
    return f"{left}{body}{right}"


@lru_cache(maxsize=None)
def build(terms: tuple[str, ...]) -> re.Pattern | None:
    """One compiled alternation for a group of terms, longest first."""
    ordered = sorted({t.lower() for t in terms if t and t.strip()},
                     key=lambda t: (-len(t), t))
    if not ordered:
        return None
    return re.compile("|".join(_pattern_for(t) for t in ordered), re.IGNORECASE)


def find_spans(text: str, terms: tuple[str, ...]) -> list[tuple[str, int, int]]:
    """Every match as (term, start, end), so overlaps can be judged later."""
    pattern = build(terms)
    if not pattern or not text:
        return []
    out = []
    for match in pattern.finditer(text):
        # Collapse the whitespace a multi-word match may have absorbed, so the
        # hit can be looked up in the lexicon dict it came from.
        out.append((re.sub(r"\s+", " ", match.group(0).lower()),
                    match.start(), match.end()))
    return out


def find(text: str, terms: tuple[str, ...]) -> list[str]:
    """Every distinct term from `terms` present in `text`, in first-seen order."""
    seen: dict[str, None] = {}
    for term, _, _ in find_spans(text, terms):
        seen.setdefault(term, None)
    return list(seen)


def find_mapped(text: str, table: dict[str, str]) -> list[tuple[str, str]]:
    """Gazetteer lookup: returns (canonical_value, matched_text) pairs.

    Used for LOCATIONS / SPORTS / TEAMS and friends, where the point is not that
    a word appeared but which known entity it names.
    """
    hits = find(text, tuple(table.keys()))
    return [(table[h], h) for h in hits if h in table]


def find_mapped_spans(text: str, table: dict[str, str]):
    """As find_mapped, but keeping each hit's span."""
    return [(table[t], t, start, end)
            for t, start, end in find_spans(text, tuple(table.keys()))
            if t in table]


def suppress_contained(signals) -> list:
    """Drop any signal whose matched span sits INSIDE another signal's span.

    This is what stops a shorter term hijacking a longer one it happens to sit
    in. Three real cases from the first run, all the same shape:

        "Real Madrid"      -> the team, NOT the city Madrid
        "makeup artist"    -> the occupation, NOT the category Art
        "personal trainer" -> the trainer, NOT Education's "trainer"

    Equal spans are kept — two lexicons legitimately naming the same text (KFC
    is both a brand and a restaurant) is a real double hit, not an overlap.
    Signals with no span (emoji, hashtags, implied) are always kept.
    """
    spanned = [s for s in signals if s.span is not None]
    keep = []
    for s in signals:
        if s.span is None:
            keep.append(s)
            continue
        start, end = s.span
        covered = any(
            (o.span[0] <= start and end <= o.span[1]) and (o.span[1] - o.span[0]) > (end - start)
            for o in spanned if o is not s
        )
        if not covered:
            keep.append(s)
    return keep
