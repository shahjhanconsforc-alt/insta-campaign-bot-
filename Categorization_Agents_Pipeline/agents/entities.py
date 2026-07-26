"""Agent 5 — Entity Extraction.

Finds the named things in a bio: places, sports, teams, athletes, brands,
companies, universities, restaurants, events, occupations, hobbies. Each becomes
a Signal carrying a `facet` (what kind of thing it is) and the canonical name,
so the Tagger can render `loc:Hunza` rather than `loc:hunza`.

Entities are also how several categories get decided without the words ever
appearing: a bio naming Skardu is about Travel whether or not it says "travel",
and one naming Lahore Qalandars is about Sports. That inference is declared in
lexicon.FACET_CATEGORY and applied here as an `implied` signal, which is weaker
than a stated word on purpose — the person named a place, they didn't say they
travel.
"""
from __future__ import annotations

from .. import lexicon
from ..contracts import CleanBio, Signal
from ..matching import find_mapped_spans

# facet -> the gazetteer it is drawn from. Order matters only for readability;
# a term appearing in two tables yields two entities, which is correct (KFC is
# both a brand and a restaurant).
_GAZETTEERS: tuple[tuple[str, dict], ...] = (
    ("loc", lexicon.LOCATIONS),
    ("sport", lexicon.SPORTS),
    ("team", lexicon.TEAMS),
    ("athlete", lexicon.ATHLETES),
    ("brand", lexicon.BRANDS),
    ("university", lexicon.UNIVERSITIES),
    ("restaurant", lexicon.RESTAURANTS),
    ("event", lexicon.EVENTS),
    ("job", lexicon.OCCUPATIONS),
    ("hobby", lexicon.HOBBIES),
)


class EntityAgent:
    name = "entities"

    def run(self, bio: CleanBio) -> list[Signal]:
        signals: list[Signal] = []
        # Hashtags are searched too — "#hunza" is a location the same as the word
        # is — but the '#' is already stripped from the cleaned text, so one pass
        # over `text` plus the hashtag words covers both.
        haystack = " ".join([bio.text] + list(bio.hashtags))

        for facet, table in _GAZETTEERS:
            for canonical, matched, start, end in find_mapped_spans(haystack, table):
                signals.append(Signal(
                    subject=canonical,
                    kind="entity",
                    evidence=matched,
                    facet=facet,
                    tag=f"{facet}:{canonical}",
                    span=(start, end),
                ))
        return signals

    def imply(self, signals: list[Signal]) -> list[Signal]:
        """The categories the SURVIVING entities imply.

        Run after span suppression, not during extraction: "Real Madrid" must
        lose its `loc:Madrid` before that phantom location is allowed to imply
        Travel. This is the rule that makes any genuinely named place produce
        both `loc:<Name>` and Travel.
        """
        out: list[Signal] = []
        for s in signals:
            if s.kind != "entity" or not s.facet:
                continue
            implied = lexicon.FACET_CATEGORY.get(s.facet)
            if implied:
                out.append(Signal(subject=implied, kind="implied", evidence=s.evidence))
        return out

    def collect(self, signals: list[Signal]) -> dict[str, list[str]]:
        """The entity signals grouped by facet, de-duplicated, in first-seen
        order — the `entities` block of the verdict."""
        out: dict[str, list[str]] = {}
        for s in signals:
            if s.kind != "entity" or not s.facet:
                continue
            bucket = out.setdefault(s.facet, [])
            if s.subject not in bucket:
                bucket.append(s.subject)
        return out
