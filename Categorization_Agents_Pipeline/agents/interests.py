"""Agent 3 — Interest Extraction.

Reads a cleaned bio and emits one Signal per piece of evidence it finds for what
this person is into. It does NOT decide categories — that is the Classifier's
job — it only reports what it saw and how it saw it, which is what lets the
Confidence Agent tell "said it twice in the same way" from "said it two
different ways".

Four independent channels, each its own `kind` (and so its own weight):

    phrase   a multi-word lexicon term    "real estate", "content creator"
    exact    a single-word lexicon term   "photographer", "cricket"
    hashtag  #travel — deliberate but noisy self-labelling
    emoji    ✈️ — a hint on its own, decisive when a word agrees
"""
from __future__ import annotations

from .. import lexicon, matching
from ..contracts import CleanBio, Signal


class InterestAgent:
    name = "interests"

    def run(self, bio: CleanBio) -> list[Signal]:
        signals: list[Signal] = []

        # 1-2. Lexicon terms in the bio text. Spans are carried so the pipeline
        #      can drop a word that only matched inside a longer phrase.
        for category, terms in lexicon.CATEGORY_TERMS.items():
            for hit, start, end in matching.find_spans(bio.text, terms):
                signals.append(Signal(
                    subject=category,
                    kind="phrase" if " " in hit else "exact",
                    evidence=hit,
                    span=(start, end),
                ))

        # 3. Hashtags. Matched against the same lexicon but scored lower: a
        #    hashtag is a claim about reach as often as about the person.
        if bio.hashtags:
            tag_text = " ".join(bio.hashtags)
            for category, terms in lexicon.CATEGORY_TERMS.items():
                for hit in matching.find(tag_text, terms):
                    signals.append(Signal(subject=category, kind="hashtag",
                                          evidence=f"#{hit}"))

        # 4. Emoji. Matched by the base character so skin-tone and
        #    variation-selector suffixes (U+FE0F and friends) still land.
        for ch in bio.emojis:
            category = lexicon.CATEGORY_EMOJI.get(ch)
            if category is None:
                category = lexicon.CATEGORY_EMOJI.get(ch[:1])
            if category:
                signals.append(Signal(subject=category, kind="emoji", evidence=ch))

        return signals

    def interests(self, signals: list[Signal]) -> list[str]:
        """The distinct evidence strings — the raw 'interests' of the profile,
        kept for the record so a verdict can be read back against the bio."""
        out: dict[str, None] = {}
        for s in signals:
            if s.kind in ("exact", "phrase"):
                out.setdefault(s.evidence, None)
        return list(out)
