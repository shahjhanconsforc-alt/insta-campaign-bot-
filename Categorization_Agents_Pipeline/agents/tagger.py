"""Agent 6 — Tag Generation.

Renders the surviving conclusions as the flat tag list the output format calls
for: bare category tags plus `facet:Value` tags for the named entities.

    Travel
    loc:Hunza
    loc:Skardu
    sport:Cricket
    team:Lahore Qalandars
    photography

Ordering is not cosmetic — it is what makes the list readable at a glance and
diffable between runs: categories first (strongest first), then entities grouped
by facet in a fixed order, alphabetical within a facet.

The location rule is enforced here as well as inferred upstream: if any `loc:`
tag survives, the bare `Travel` tag is guaranteed present. The Entity agent
already emits an implied Travel signal for a place, but that signal can be cut
by the confidence threshold, and the rule is specified as absolute — a bio that
mentions a place gets both tags.
"""
from __future__ import annotations

from .. import config, lexicon
from ..contracts import Signal

# The order facets appear in the tag list.
_FACET_ORDER = ("loc", "sport", "team", "athlete", "event", "brand",
                "university", "restaurant", "job", "hobby")


class TaggerAgent:
    name = "tagger"

    def run(self, signals: list[Signal], categories: list[str],
            scored: dict[str, float]) -> list[str]:
        tags: list[str] = []

        # 1. Categories, in the order the classifier settled on.
        for category in categories:
            tag = category if category != "Photography" else "photography"
            tags.append(tag)

        # 2. Entities that cleared the threshold, grouped by facet.
        by_facet: dict[str, set[str]] = {}
        for s in signals:
            if s.kind != "entity" or not s.facet or not s.tag:
                continue
            if scored.get(s.subject, 0.0) < config.MIN_CONFIDENCE:
                continue
            by_facet.setdefault(s.facet, set()).add(s.subject)

        for facet in _FACET_ORDER:
            for value in sorted(by_facet.get(facet, ())):
                tags.append(f"{facet}:{value}")

        # 3. The location rule, made absolute: any place named means Travel.
        if any(t.startswith("loc:") for t in tags) and "Travel" not in tags:
            tags.insert(0, "Travel")

        # De-duplicate while keeping the order decided above.
        seen: dict[str, None] = {}
        for tag in tags:
            seen.setdefault(tag, None)
        return list(seen)[:config.MAX_TAGS]

    def categories_for(self, tags: list[str]) -> list[str]:
        """The bare (non-prefixed) tags — used to keep the stored category list
        and tag list from ever contradicting each other."""
        out = []
        for tag in tags:
            if ":" in tag:
                continue
            canonical = "Photography" if tag == "photography" else tag
            if canonical in lexicon.CATEGORIES:
                out.append(canonical)
        return out
