"""Accuracy evaluation harness (FYP Objective 4: MER / ROUGE / METEOR).

Scores the system's output for ONE meeting against human-written reference
files, using the metrics named in the IR:

  Transcription  -> WER (word error rate) and MER (Mixed Error Rate, the
                    code-switch-aware metric: Chinese is scored per character,
                    Latin script per word).
  Summarization  -> ROUGE-1, ROUGE-L (F1) and METEOR.

Everything runs locally. The core metrics are pure-Python (no installs).
METEOR uses nltk + WordNet if available; otherwise it is reported as "n/a"
with a one-line hint (it is the only metric that needs an extra package).

USAGE
  # prove the metric math is correct (no data needed):
  python tests\\evaluate_accuracy.py --selftest

  # score a real meeting against your reference files:
  python tests\\evaluate_accuracy.py <meeting_id> <reference_dir>

REFERENCE FILES (place inside <reference_dir>):
  reference_transcript.txt   what was ACTUALLY said (plain text)   -> WER, MER
  reference_summary.txt      a gold/human summary of the meeting   -> ROUGE, METEOR
Either file may be omitted; the harness scores whatever is present.

The system's "hypothesis" is read straight from the database: the transcript
is every segment's text in time order; the summary is meetings.summary.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"

# METEOR needs WordNet. nltk 3.9's security check refuses to read WordNet from a
# .zip, so it must be present UNZIPPED. We keep an unzipped copy inside the venv
# and make sure that path is searched. If METEOR ever reports n/a again, unzip
# %APPDATA%\nltk_data\corpora\wordnet.zip into .venv\nltk_data\corpora\ (see
# docs/EVALUATION_GUIDE.md).
_LOCAL_NLTK = Path(__file__).resolve().parent.parent / ".venv" / "nltk_data"
try:
    import nltk as _nltk
    if str(_LOCAL_NLTK) not in _nltk.data.path:
        _nltk.data.path.insert(0, str(_LOCAL_NLTK))
except Exception:
    pass

# Chinese comes in Traditional and Simplified scripts; the same word written in
# the two scripts (国/國) is the same word but different characters, so a naive
# char-level comparison would count every style difference as an error. Fold both
# sides to Simplified before scoring so the metric reflects real transcription
# divergence, not script style. No-op for English (opencc only maps CJK).
try:
    from opencc import OpenCC as _OpenCC
    _T2S = _OpenCC("t2s")
    def _to_simplified(text: str) -> str:
        return _T2S.convert(text)
except Exception:
    def _to_simplified(text: str) -> str:
        return text

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation (keep word chars + CJK), collapse spaces."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def word_tokens(text: str) -> list[str]:
    """Whitespace word tokens (the classic WER unit; English-oriented)."""
    return normalize(text).split()


def mixed_tokens(text: str) -> list[str]:
    """Code-switch tokens: each CJK char is a token, Latin runs stay whole words.

    This is the unit behind Mixed Error Rate - it scores Mandarin at the
    character level (where 'words' have no spaces) and English at the word level.
    """
    out: list[str] = []
    for chunk in normalize(text).split():
        buf = ""
        for ch in chunk:
            if _CJK.match(ch):
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)
            else:
                buf += ch
        if buf:
            out.append(buf)
    return out


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    """Levenshtein distance (substitutions + insertions + deletions) on tokens."""
    n, m = len(ref), len(hyp)
    if n == 0:
        return m
    dp = list(range(m + 1))
    for i in range(1, n + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, m + 1):
            cur = dp[j]
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + cost)
            prev = cur
    return dp[m]


def error_rate(ref: list[str], hyp: list[str]) -> float:
    """Edit distance / reference length. 0.0 = perfect; can exceed 1.0."""
    return float("nan") if not ref else edit_distance(ref, hyp) / len(ref)


def rouge_1(ref: list[str], hyp: list[str]) -> tuple[float, float, float]:
    """Unigram overlap precision / recall / F1."""
    overlap = sum((Counter(ref) & Counter(hyp)).values())
    p = overlap / len(hyp) if hyp else 0.0
    r = overlap / len(ref) if ref else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def _lcs(a: list[str], b: list[str]) -> int:
    m = len(b)
    dp = [0] * (m + 1)
    for i in range(len(a)):
        prev = 0
        for j in range(1, m + 1):
            tmp = dp[j]
            dp[j] = prev + 1 if a[i] == b[j - 1] else max(dp[j], dp[j - 1])
            prev = tmp
    return dp[m]


def rouge_l(ref: list[str], hyp: list[str]) -> tuple[float, float, float]:
    """Longest-common-subsequence precision / recall / F1."""
    lcs = _lcs(ref, hyp)
    p = lcs / len(hyp) if hyp else 0.0
    r = lcs / len(ref) if ref else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def meteor(ref_text: str, hyp_text: str) -> float | None:
    """METEOR via nltk+WordNet if available; None if the package/corpus is missing."""
    try:
        from nltk.translate.meteor_score import meteor_score
    except ImportError:
        return None
    try:
        return float(meteor_score([word_tokens(ref_text)], word_tokens(hyp_text)))
    except LookupError:
        return None  # WordNet corpus not downloaded


# ----------------------------------------------------------------------------
# Hypothesis extraction (from the DB) + reference loading (from files)
# ----------------------------------------------------------------------------

def load_hypothesis(meeting_id: int, sample_minutes: float | None = None) -> tuple[str, str]:
    """Return (transcript_text, summary_text) for a meeting from the DB.

    When `sample_minutes` is given, the transcript is truncated to the segments
    whose start time is within the first N minutes. This lets a long meeting be
    scored against a short, manually-corrected reference excerpt (the reference
    must cover the SAME first-N-minutes span). The summary is always the whole
    meeting's summary - summary scoring is not sampled.
    """
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found at {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    m = con.execute("SELECT summary FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if m is None:
        raise SystemExit(f"Meeting {meeting_id} not found")
    if sample_minutes is not None:
        # window is relative to the meeting's OWN first segment - some meetings
        # (e.g. 27, 31) don't start their timestamps at zero, so an absolute
        # cut-off would miss everything.
        first = con.execute(
            "SELECT MIN(start_seconds) AS s FROM segments WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()["s"] or 0.0
        segs = con.execute(
            "SELECT text FROM segments WHERE meeting_id = ? AND start_seconds < ? "
            "ORDER BY start_seconds",
            (meeting_id, first + sample_minutes * 60.0),
        ).fetchall()
    else:
        segs = con.execute(
            "SELECT text FROM segments WHERE meeting_id = ? ORDER BY start_seconds",
            (meeting_id,),
        ).fetchall()
    con.close()
    transcript = " ".join(s["text"] for s in segs)
    return transcript, (m["summary"] or "")


def _pct(x: float) -> str:
    return "n/a" if x != x else f"{x * 100:.2f}%"  # x!=x detects NaN


def align_hypothesis_to_reference(full_hyp: str, ref_text: str, anchor: int = 25):
    """Truncate the system transcript to the SAME span the reference covers.

    Used with an EXTERNAL reference (e.g. a commercial tool) that only transcribed
    the opening N minutes: both start at the meeting's beginning, so we find where
    the reference's ENDING aligns inside the (much longer) system transcript and
    cut there. Alignment is by the reference's last `anchor` tokens, matched by
    token-set overlap over a sliding window - robust to the two systems' different
    word densities and to transcription errors in either. Returns
    (truncated_hyp_text, overlap_score, anchor_len).
    """
    hyp_mt = mixed_tokens(full_hyp)
    ref_mt = mixed_tokens(ref_text)
    if len(ref_mt) < anchor or not hyp_mt:
        return full_hyp, -1, 0
    tail = ref_mt[-anchor:]
    tail_set = set(tail)
    best_i, best_score = len(hyp_mt), -1
    for i in range(anchor, len(hyp_mt) + 1):
        score = len(tail_set & set(hyp_mt[i - anchor:i]))
        if score > best_score:
            best_score, best_i = score, i
    return " ".join(hyp_mt[:best_i]), best_score, anchor


def evaluate(meeting_id: int, reference_dir: Path, sample_minutes: float | None = None,
             align: bool = False) -> None:
    # --align uses the FULL system transcript, then truncates it to the external
    # reference's span; otherwise honour --sample-minutes.
    transcript_hyp, summary_hyp = load_hypothesis(meeting_id, None if align else sample_minutes)
    ref_tx = reference_dir / "reference_transcript.txt"
    ref_sum = reference_dir / "reference_summary.txt"

    print(f"=== Accuracy evaluation - meeting {meeting_id} ===")
    print(f"reference dir: {reference_dir}")
    if align:
        print("transcript aligned to the external reference's span "
              "(divergence vs a commercial baseline, not vs verbatim ground truth)")
    elif sample_minutes is not None:
        print(f"transcript scored on the first {sample_minutes:g} minute(s) "
              f"(reference must cover the same span); summary scored in full")

    if ref_tx.exists():
        ref_text = ref_tx.read_text(encoding="utf-8")
        if align:
            transcript_hyp, ov, alen = align_hypothesis_to_reference(transcript_hyp, ref_text)
            print(f"  span alignment: matched {ov}/{alen} tail tokens "
                  f"({'strong' if ov >= alen * 0.5 else 'WEAK - check'})")
        if _CJK.search(ref_text):
            ref_text = _to_simplified(ref_text)
            transcript_hyp = _to_simplified(transcript_hyp)
            print("  Chinese: both folded to Simplified before scoring")
        wer = error_rate(word_tokens(ref_text), word_tokens(transcript_hyp))
        mer = error_rate(mixed_tokens(ref_text), mixed_tokens(transcript_hyp))
        print("\n-- Transcription --")
        print(f"  reference words : {len(word_tokens(ref_text))}")
        print(f"  hypothesis words: {len(word_tokens(transcript_hyp))}")
        print(f"  WER             : {_pct(wer)}")
        print(f"  MER (mixed)     : {_pct(mer)}")
    else:
        print(f"\n-- Transcription -- (skipped: no {ref_tx.name})")

    if ref_sum.exists():
        ref_text = ref_sum.read_text(encoding="utf-8")
        if _CJK.search(ref_text):
            ref_text = _to_simplified(ref_text)
            summary_hyp = _to_simplified(summary_hyp)
        r1 = rouge_1(mixed_tokens(ref_text), mixed_tokens(summary_hyp))
        rl = rouge_l(mixed_tokens(ref_text), mixed_tokens(summary_hyp))
        met = meteor(ref_text, summary_hyp)
        print("\n-- Summarization --")
        print(f"  ROUGE-1 (P/R/F) : {r1[0]*100:.2f}% / {r1[1]*100:.2f}% / {r1[2]*100:.2f}%")
        print(f"  ROUGE-L (P/R/F) : {rl[0]*100:.2f}% / {rl[1]*100:.2f}% / {rl[2]*100:.2f}%")
        print(f"  METEOR          : {f'{met:.4f}' if met is not None else 'n/a (pip install nltk + nltk.download(\"wordnet\",\"omw-1.4\"))'}")
    else:
        print(f"\n-- Summarization -- (skipped: no {ref_sum.name})")


# ----------------------------------------------------------------------------
# Self-test: verify the metric math on hand-computed examples (no data needed)
# ----------------------------------------------------------------------------

def selftest() -> int:
    checks: list[tuple[str, bool]] = []

    def approx(a, b, name, tol=1e-6):
        checks.append((name, abs(a - b) <= tol))

    # WER: 1 deletion ("on") out of 6 reference words -> 1/6
    approx(error_rate(word_tokens("the cat sat on the mat"),
                       word_tokens("the cat sat the mat")), 1 / 6, "WER 1 deletion")
    # WER: 1 substitution -> 1/3
    approx(error_rate(word_tokens("open the door"),
                      word_tokens("open the window")), 1 / 3, "WER 1 substitution")
    # MER on Chinese: ref 6 chars, hyp drops 1 char -> 1/6
    approx(error_rate(mixed_tokens("我们今天开会"),
                      mixed_tokens("我们今天开")), 1 / 6, "MER 1 char deletion")
    # MER mixed EN+ZH: tokens = ['hello','世','界'] vs ['hello','世'] -> 1/3
    approx(error_rate(mixed_tokens("hello 世界"),
                      mixed_tokens("hello 世")), 1 / 3, "MER mixed deletion")
    # mixed_tokens splits CJK to chars but keeps English whole
    checks.append(("mixed tokenization", mixed_tokens("hi 世界") == ["hi", "世", "界"]))
    # ROUGE-1 identical -> F1 = 1.0
    approx(rouge_1(word_tokens("a b c"), word_tokens("a b c"))[2], 1.0, "ROUGE-1 identical")
    # ROUGE-1 half overlap: ref 'a b c d', hyp 'a b' -> P=1,R=0.5,F=2/3
    approx(rouge_1(word_tokens("a b c d"), word_tokens("a b"))[2], 2 / 3, "ROUGE-1 partial F1")
    # ROUGE-L: ref 'a b c d', hyp 'a c d' -> LCS=3; P=1,R=0.75,F=6/7
    approx(rouge_l(word_tokens("a b c d"), word_tokens("a c d"))[2], 6 / 7, "ROUGE-L subsequence")
    # error_rate handles empty reference
    checks.append(("empty ref -> NaN", error_rate([], ["x"]) != error_rate([], ["x"])))

    print("=== evaluate_accuracy self-test ===")
    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(checks)} passed")
    met = meteor("the cat sat on the mat", "the cat sat on the mat")
    print(f"METEOR available: {'yes' if met is not None else 'no (optional; pip install nltk + wordnet)'}")
    return 0 if passed == len(checks) else 1


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] == "--selftest":
        return selftest()
    # optional: --sample-minutes N  (score the transcript on the first N minutes)
    sample_minutes: float | None = None
    align = False
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--sample-minutes" and i + 1 < len(args):
            sample_minutes = float(args[i + 1])
            i += 2
        elif args[i] == "--align":
            align = True
            i += 1
        else:
            positional.append(args[i])
            i += 1
    if len(positional) < 2:
        print("usage: python tests\\evaluate_accuracy.py <meeting_id> <reference_dir> "
              "[--sample-minutes N] [--align]")
        print("   --align: reference is an external transcript covering only the opening;")
        print("            the system transcript is truncated to that same span.")
        print("   or: python tests\\evaluate_accuracy.py --selftest")
        return 2
    evaluate(int(positional[0]), Path(positional[1]), sample_minutes, align)
    return 0


if __name__ == "__main__":
    sys.exit(main())
