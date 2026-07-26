"""Tunables for the categorization pipeline.

Everything a run can be adjusted by lives here, so the agents themselves stay
pure logic and a future job can be re-pointed without editing them.
"""
from __future__ import annotations

# ── Confidence ───────────────────────────────────────────────────────────
# A tag/category below this is computed but NOT saved. The Confidence Agent is
# the only thing that reads it. 0.55 keeps single-strong-signal hits (an explicit
# keyword, a named place) while dropping lone weak ones (a bare emoji).
MIN_CONFIDENCE = 0.55

# Evidence strengths. A match's confidence starts at the weight of the signal
# that found it and rises as independent signals agree — two different kinds of
# evidence for the same category is much stronger than the same one twice.
WEIGHT = {
    "exact":    0.92,   # a whole-word lexicon term ("photographer", "cricket")
    "phrase":   0.95,   # a multi-word phrase ("real estate", "content creator")
    "entity":   0.90,   # a gazetteer hit (a city, a team, a university)
    "hashtag":  0.80,   # #travel — deliberate self-labelling, but noisy
    "emoji":    0.62,   # ✈️ alone is a hint, not a statement
    "implied":  0.70,   # inferred, e.g. Travel because a location was named
}

# How much each extra INDEPENDENT signal adds (diminishing, capped below).
AGREEMENT_BONUS = 0.05
MAX_CONFIDENCE = 0.99

# ── Concurrency ──────────────────────────────────────────────────────────
# Workers pulling from one shared queue. The per-profile work is regex over a
# short string — sub-millisecond — so this is about pipeline shape and keeping
# the store I/O overlapped, not about saturating cores.
DEFAULT_WORKERS = 8
MAX_WORKERS = 64

# Verdicts are written in batches: every save is a full read-modify-write of the
# MinIO object, so saving one at a time would be quadratic over thousands of rows.
SAVE_EVERY = 500

# ── Input ────────────────────────────────────────────────────────────────
# A bio shorter than this carries no signal worth the work — they are
# overwhelmingly a bare emoji or a link.
MIN_BIO_CHARS = 3

# The bio store's "no bio was read for this handle" sentinel. Such a row has
# nothing to categorize, so the loader skips it rather than inventing a guess.
UNKNOWN_BIO = "unknown"

# ── Output ───────────────────────────────────────────────────────────────
# Categories worth keeping per profile, best first. Instagram bios rarely support
# more than a handful honestly; the cap stops a long bio turning into confetti.
MAX_CATEGORIES = 6
MAX_TAGS = 40
