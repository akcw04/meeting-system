"""Re-run LLM categorization for one or more meetings and PERSIST the result.

A maintenance / retry utility (like delete_meeting.py): re-extracts insights with
the CURRENT categorize.py code and overwrites the stored insights + summary for
each meeting id given. Useful after improving the prompt/extraction - it replaces
the old stored output WITHOUT needing the API or a backend restart (it writes the
DB directly via the same run_categorization() the pipeline uses).

Idempotent per meeting (run_categorization clears old insights first). Runs the
local LLM via Ollama, so Ollama must be up; it is as slow as the GPU (bug #26).

USAGE
  python tests\\recategorize_meeting.py 28
  python tests\\recategorize_meeting.py 28 20 31
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from app.pipeline.runner import run_categorization  # noqa: E402


def main() -> int:
    ids = sys.argv[1:]
    if not ids:
        print("usage: python tests\\recategorize_meeting.py <meeting_id> [<meeting_id> ...]")
        return 2
    failed: list[str] = []
    for raw in ids:
        try:
            mid = int(raw)
        except ValueError:
            print(f"skip {raw!r}: not an integer meeting id")
            failed.append(raw)
            continue
        print(f"\n=== re-categorizing meeting {mid} ===")
        try:
            run_categorization(mid)
            print(f"meeting {mid}: DONE (status -> ready; insights + summary persisted)")
        except Exception as exc:  # noqa: BLE001
            print(f"meeting {mid}: FAILED - {type(exc).__name__}: {exc}")
            failed.append(raw)
    if failed:
        print(f"\n{len(failed)} failed: {', '.join(map(str, failed))}")
        return 1
    print("\nAll requested meetings re-categorized.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
