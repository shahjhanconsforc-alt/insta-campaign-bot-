"""Agent 7 — Confidence.

Turns a pile of Signals into a score per conclusion, and applies the cut.

The scoring rule is deliberately simple and explainable: a conclusion starts at
the weight of its STRONGEST signal, then gains a diminishing bonus for each
additional signal of a DIFFERENT kind. Repeating the same kind adds nothing —
a bio that says "travel" twice is not more about travel than one that says it
once, whereas a bio that says "travel" AND carries ✈️ AND names Skardu is.

Everything below config.MIN_CONFIDENCE is dropped here and never reaches the
writer, which is the whole point of the agent: one place decides what is worth
saving.
"""
from __future__ import annotations

from .. import config
from ..contracts import Signal


class ConfidenceAgent:
    name = "confidence"

    def score(self, signals: list[Signal]) -> dict[str, float]:
        """A confidence per subject, from the signals supporting it."""
        by_subject: dict[str, set[str]] = {}
        for s in signals:
            by_subject.setdefault(s.subject, set()).add(s.kind)

        scored: dict[str, float] = {}
        for subject, kinds in by_subject.items():
            weights = sorted((config.WEIGHT.get(k, 0.5) for k in kinds), reverse=True)
            value = weights[0]
            # Independent corroboration: each further KIND of evidence nudges it
            # up by a fixed step, so agreement matters but can't run away.
            value += config.AGREEMENT_BONUS * (len(weights) - 1)
            scored[subject] = round(min(value, config.MAX_CONFIDENCE), 3)
        return scored

    def keep(self, scored: dict[str, float]) -> dict[str, float]:
        """Only what clears the threshold, best first."""
        passing = {k: v for k, v in scored.items() if v >= config.MIN_CONFIDENCE}
        return dict(sorted(passing.items(), key=lambda kv: (-kv[1], kv[0])))
