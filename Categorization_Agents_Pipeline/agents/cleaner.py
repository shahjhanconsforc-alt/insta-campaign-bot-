"""Agent 2 — Bio Cleaning.

Normalizes a bio for matching WITHOUT throwing away the parts that carry
meaning. An Instagram bio says as much through its emoji, hashtags and mentions
as through its sentences, so those are extracted into their own fields rather
than stripped — each is separately weighted evidence later.

What normalization does: fold case, strip URLs and contact details (a phone
number or an email is noise for interest matching, and "gmail" would otherwise
read as Technology), replace punctuation and separator glyphs with spaces so
"Travel•Food•Photography" splits into words, and collapse whitespace. Emoji are
left in `raw` and pulled into their own list; the matching `text` drops them.
"""
from __future__ import annotations

import re
import unicodedata

from ..contracts import CleanBio, Profile

_URL = re.compile(r"(https?://\S+|www\.\S+|\b\S+\.(?:com|net|org|io|co|pk|uk|me|app|link)\b/?\S*)",
                  re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_HASHTAG = re.compile(r"#(\w+)")
_MENTION = re.compile(r"@([\w.]+)")

# Bullets, arrows, box glyphs and the rest of the separator zoo bios are built
# from. Turned into spaces so the words either side become separate tokens.
_SEPARATORS = re.compile(r"[•·▪◆▶►|/\\\-–—_+~*=<>\[\]{}()\"'’“”:;,!?.]+")

# Scripts other than Latin, used as a crude language hint. Deliberately crude:
# the pipeline matches English/Roman-script terms, so all this decides is
# whether a bio is worth telling the operator it may be under-read.
_SCRIPTS = (
    ("ar", re.compile(r"[؀-ۿ]")),      # Arabic / Urdu
    ("hi", re.compile(r"[ऀ-ॿ]")),      # Devanagari
    ("zh", re.compile(r"[一-鿿]")),      # CJK
    ("ja", re.compile(r"[぀-ヿ]")),      # Kana
    ("ko", re.compile(r"[가-힯]")),      # Hangul
    ("ru", re.compile(r"[Ѐ-ӿ]")),      # Cyrillic
)


def _is_emoji(ch: str) -> bool:
    """Pictographic characters, which `unicodedata` marks So (symbol, other)."""
    return unicodedata.category(ch) == "So" or ord(ch) in range(0x1F000, 0x1FAFF)


class BioCleaningAgent:
    name = "cleaner"

    def run(self, profile: Profile) -> CleanBio:
        raw = profile.bio or ""

        hashtags = [h.lower() for h in _HASHTAG.findall(raw)]
        mentions = [m.lower() for m in _MENTION.findall(raw)]
        emojis = [ch for ch in raw if _is_emoji(ch)]

        text = raw
        # Order matters: emails before phones (an email holds digits), and both
        # before URLs strip the domains they share.
        text = _EMAIL.sub(" ", text)
        text = _URL.sub(" ", text)
        text = _PHONE.sub(" ", text)
        # A hashtag's WORD is real evidence, so keep it and drop only the '#'.
        # A mention is someone else's handle — rarely about this person, so it goes.
        text = _MENTION.sub(" ", text)
        text = _HASHTAG.sub(r" \1 ", text)
        text = "".join(" " if _is_emoji(ch) else ch for ch in text)
        text = _SEPARATORS.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip().lower()

        return CleanBio(
            username=profile.username,
            raw=raw,
            text=text,
            tokens=[t for t in text.split() if t],
            emojis=emojis,
            hashtags=hashtags,
            mentions=mentions,
            language=self.detect_language(raw, text),
        )

    def detect_language(self, raw: str, text: str) -> str:
        """A script-based guess, or "en" when it reads as Latin, or "und"."""
        for code, pattern in _SCRIPTS:
            if pattern.search(raw):
                return code
        if any(ch.isalpha() for ch in text):
            return "en"
        return "und"
