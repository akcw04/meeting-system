"""Regression test for bug #21 - the consolidation (reduce) step.

Meeting 20's real run produced ZERO items in every category because the
LLM mangled segment ids while merging chunk extractions, and the
grounding filter deleted everything. The fix: the model now references
inputs by small item_id and we rebuild segment citations in code.

This test forces the 13-line synthetic standup through MULTIPLE chunks
(by shrinking the chunk budget) so the consolidation call actually runs,
then asserts the planted facts SURVIVE with valid segment citations.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from app.pipeline import categorize  # noqa: E402
from tests.smoke_phase7 import FAKE_SEGMENTS, FAKE_SPEAKERS  # noqa: E402


def main() -> int:
    print("=== Phase 7b: multi-chunk consolidation regression (bug #21) ===")

    # Shrink the chunk budget so the standup splits into several chunks.
    original_budget = categorize.CHUNK_TOKEN_BUDGET
    categorize.CHUNK_TOKEN_BUDGET = 120  # ~480 chars -> 3-4 chunks
    try:
        t0 = time.time()
        insights = categorize.categorize_transcript(FAKE_SEGMENTS, FAKE_SPEAKERS, "en")
        elapsed = time.time() - t0
    finally:
        categorize.CHUNK_TOKEN_BUDGET = original_budget

    print(f"\nDone in {elapsed:.1f}s")
    print(f"summary ({len(insights.summary.split())} words): {insights.summary[:160]}...")
    counts = {
        "action_items": len(insights.action_items),
        "decisions": len(insights.decisions),
        "deadlines": len(insights.deadlines),
        "issues": len(insights.issues),
        "risks": len(insights.risks),
    }
    print("counts:", counts)
    for it in insights.action_items:
        print(f"  AI: {it.description}  (segs {it.source_segment_ids})")
    for it in insights.decisions:
        print(f"  DE: {it.description}  (segs {it.source_segment_ids})")

    # The whole point: items must SURVIVE consolidation...
    assert counts["action_items"] >= 1, "Consolidation lost all action items (bug #21 regressed!)"
    assert counts["decisions"] >= 1, "Consolidation lost all decisions (bug #21 regressed!)"
    assert sum(counts.values()) >= 4, f"Suspiciously few items after consolidation: {counts}"

    # ...and their citations must be REAL segment ids.
    valid_ids = {s["id"] for s in FAKE_SEGMENTS}
    for group in (insights.action_items, insights.decisions, insights.deadlines,
                  insights.issues, insights.risks):
        for it in group:
            assert it.source_segment_ids, f"Item with no citation: {it.description!r}"
            assert all(i in valid_ids for i in it.source_segment_ids), (
                f"Invalid segment id in {it.description!r}: {it.source_segment_ids}"
            )

    assert len(insights.summary.split()) >= 20, "Summary too short"
    print("\nPhase 7b multi-chunk regression PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
