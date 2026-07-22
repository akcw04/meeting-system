"""Lightweight citation-support check (FLAG, never drop).

Flags an insight item whose cited transcript segment shares no meaningful
content with the item's wording - a cheap signal that the citation may not
actually back the claim (e.g. the mtg-28 "payment window" item that cited a
segment about postponing). The UI shows a "verify" hint; nothing is removed.

Deliberately CONSERVATIVE to avoid false-positive noise: when the item and the
cited text are in different scripts (an English item citing a Chinese segment -
common in code-switch meetings) we cannot compare words, so we do NOT flag.
Better a missed flag than a noisy one. So this verifies SAME-language citations
(the common case + pure-EN/ZH meetings); cross-language ones are left unflagged
(a documented limitation).
"""
from __future__ import annotations

import re

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[A-Za-z]{4,}")
_CJK = re.compile(r"[一-鿿㐀-䶿]")
# Common words that carry no topical signal - excluded so two unrelated lines
# don't look "supported" just because both say "should" or "meeting".
_STOP = {
    "will", "with", "that", "this", "from", "have", "should", "shall", "must",
    "their", "they", "them", "about", "into", "than", "then", "there", "these",
    "those", "your", "yours", "been", "before", "after", "meeting", "team",
    "make", "made", "need", "needs", "ensure", "going", "want", "wants", "also",
}


def _signals(text: str) -> tuple[set[str], set[str], set[str]]:
    """(numbers, content words >=4 chars sans stopwords, CJK chars) in `text`."""
    low = text.lower()
    nums = {m.replace(",", "") for m in _NUM.findall(low)}  # "5,000" == "5000"
    # 4-char prefix folds morphological variants (safe/safety, remedy/remedied,
    # finalize/finalise) so they count as a match - biasing AWAY from noisy flags.
    words = {w[:4] for w in _WORD.findall(low) if w not in _STOP}
    cjk = set(_CJK.findall(text))
    return nums, words, cjk


def is_low_support(description: str, segment_text: str | None) -> bool:
    """True iff the cited segment shares NO number / content word / CJK char with
    the item description AND a same-script comparison was actually possible."""
    if not description or not segment_text:
        return False
    d_num, d_word, d_cjk = _signals(description)
    s_num, s_word, s_cjk = _signals(segment_text)
    if (d_num & s_num) or (d_word & s_word) or (d_cjk & s_cjk):
        return False  # at least one shared signal -> treat as supported
    # No overlap. Only flag when both sides have comparable tokens of the same
    # script; a Latin-only description vs a CJK-only segment can't be judged.
    same_script_comparable = (bool(d_word) and bool(s_word)) or (bool(d_cjk) and bool(s_cjk))
    return same_script_comparable
