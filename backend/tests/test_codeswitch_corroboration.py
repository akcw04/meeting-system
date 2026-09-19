"""Smoke suite: the code-switch corroboration guard.

Every case below is taken from real meetings in the project database, so the
thresholds are checked against measured behaviour rather than invented numbers:

  m260  Self Recorded.mp4   34 segments, en dominant
        ms  2 (5.9%)  <- FALSE: two English lines mis-detected as Malay
        zh  2 (5.9%)  <- TRUE:  genuine Mandarin, proved by its script
  m230  malay chinese english mix.mp4   45 segments, en dominant
        ms  8 (17.8%) <- TRUE
        zh  5 (11.1%) <- TRUE
  m76   Kairos test 2   41 segments, en dominant
        ms  8 (19.5%) <- TRUE
        zh  4 (9.8%)  <- TRUE

Run directly:  .venv\\Scripts\\python.exe tests\\test_codeswitch_corroboration.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline.transcribe import (  # noqa: E402
    _drop_uncorroborated,
    _shares_script,
)

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {extra}")


def kept(by_lang, dominant, total):
    return sorted(_drop_uncorroborated(by_lang, dominant, total))


print("--- script families ---")
check("zh does not share script with en", not _shares_script("zh", "en"))
check("ms shares script with en", _shares_script("ms", "en"))
check("en shares script with ms", _shares_script("en", "ms"))
check("en does not share script with zh", not _shares_script("en", "zh"))

print("\n--- m260: the bug being fixed ---")
m260 = {"ms": list(range(2)), "zh": list(range(2))}
got = kept(m260, "en", 34)
check("spurious Malay (2/34) is dropped", "ms" not in got, got)
check("genuine Mandarin (2/34) is kept on script", "zh" in got, got)
check("result is Mandarin only", got == ["zh"], got)

print("\n--- m230 / m76: genuine trilingual must survive ---")
got = kept({"ms": list(range(8)), "zh": list(range(5))}, "en", 45)
check("m230 keeps both ms (17.8%) and zh", got == ["ms", "zh"], got)
got = kept({"ms": list(range(8)), "zh": list(range(4))}, "en", 41)
check("m76 keeps both ms (19.5%) and zh", got == ["ms", "zh"], got)

print("\n--- threshold behaviour ---")
check("same-script at 2/34 (5.9%) dropped",
      kept({"ms": list(range(2))}, "en", 34) == [])
check("same-script at 3/34 (8.8%) dropped - share bar",
      kept({"ms": list(range(3))}, "en", 34) == [])
check("same-script at 4/34 (11.8%) kept",
      kept({"ms": list(range(4))}, "en", 34) == ["ms"])
check("same-script at 2/10 (20%) dropped - count bar",
      kept({"ms": list(range(2))}, "en", 10) == [])
check("different-script at 1/100 still kept",
      kept({"zh": [0]}, "en", 100) == ["zh"])

print("\n--- symmetry and edge cases ---")
check("English minority inside a Malay meeting needs corroboration too",
      kept({"en": list(range(2))}, "ms", 34) == [])
check("English inside a Mandarin meeting is kept on script",
      kept({"en": [0]}, "zh", 40) == ["en"])
check("empty input is safe", kept({}, "en", 34) == [])
check("zero segments does not divide by zero",
      kept({"ms": [0]}, "en", 0) == ["ms"])

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
