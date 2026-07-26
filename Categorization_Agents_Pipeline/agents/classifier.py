"""Agent 4 — Category Classification.

Decides which of the canonical categories a profile belongs to, from the Signals
the Interest and Entity agents produced and the scores the Confidence agent gave
them. A profile may hold several categories; that is normal and wanted.

Two judgement calls live here rather than in the lexicon, because both are about
the SET of categories rather than any one of them:

  * Sports is dropped when a specific sport was found. `sport:Cricket` already
    says it, and the spec asks for the specific tag rather than only the generic
    one — but Sports is kept as a category, since the category list is what the
    panels segment on. What is dropped is the redundant *duplicate* when a bio
    yields both a bare "sports" word and a named sport: the named one wins the
    ordering.
  * Lifestyle is a catch-all. When a profile earned a substantive category,
    Lifestyle adds nothing and is dropped; when it earned nothing else, it is
    kept so the profile isn't left Unknown.
"""
from __future__ import annotations

from .. import config, lexicon
from ..contracts import Signal

# Categories that describe HOW someone presents rather than what they are into.
# Only ever kept when nothing more specific survived.
_GENERIC = ("Lifestyle",)


class ClassifierAgent:
    name = "classifier"

    def run(self, signals: list[Signal], scored: dict[str, float]) -> list[str]:
        """The ordered category list for one profile."""
        # Only subjects that are actual categories — the same scored dict also
        # holds entity subjects like "Hunza", which are tags, not categories.
        cats = {s: v for s, v in scored.items() if s in lexicon.CATEGORIES}
        if not cats:
            return []

        specific = [c for c in cats if c not in _GENERIC]
        if specific:
            cats = {c: v for c, v in cats.items() if c not in _GENERIC}

        # A category backed by a stated word outranks one merely implied by an
        # entity, at equal score — "I'm a photographer" beats "named a city".
        stated = {s.subject for s in signals if s.kind in ("exact", "phrase")}
        ordered = sorted(
            cats.items(),
            key=lambda kv: (-kv[1], kv[0] not in stated, kv[0]),
        )
        return [c for c, _ in ordered][:config.MAX_CATEGORIES]
