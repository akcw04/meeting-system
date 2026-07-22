"""Unit smoke test for the phantom-speaker merge (Phase 9 polish).

merge_phantom_speakers() folds speakers with tiny total talk-time into their
acoustically nearest neighbour, using pyannote voiceprints (cosine similarity).
This exercises the pure function with synthetic turns + embeddings - no audio,
no GPU, no diarization run - so the merge logic is verified on its own.

Run:  python tests\\smoke_phase5b_phantom.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402

from app.pipeline.diarize import merge_phantom_speakers  # noqa: E402


def turn(start: float, end: float, speaker: str) -> dict:
    return {"start_seconds": float(start), "end_seconds": float(end), "speaker": speaker}


def distinct(turns: list[dict]) -> list[str]:
    return sorted({t["speaker"] for t in turns})


def main() -> int:
    print("=== Phase 5b: phantom-speaker merge ===")
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool) -> None:
        (passed if cond else failed).append(name)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # Voiceprints: 00 and 01 are distinct directions; the phantom 02 sits right
    # next to 00 (nearly parallel) and far from 01.
    emb = {
        "SPEAKER_00": np.array([1.0, 0.0, 0.0, 0.0], dtype="float32"),
        "SPEAKER_01": np.array([0.0, 1.0, 0.0, 0.0], dtype="float32"),
        "SPEAKER_02": np.array([0.95, 0.05, 0.0, 0.0], dtype="float32"),  # ~ SPEAKER_00
    }

    # Case 1: a 1-second phantom merges into its nearest voiceprint (00).
    turns = [
        turn(0, 30, "SPEAKER_00"),
        turn(30, 60, "SPEAKER_01"),
        turn(60, 61, "SPEAKER_02"),  # 1s phantom
    ]
    out = merge_phantom_speakers(turns, emb, max_seconds=3.0)
    check("phantom (1s) removed", "SPEAKER_02" not in distinct(out))
    check(
        "phantom remapped to nearest (SPEAKER_00)",
        [t["speaker"] for t in out] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"],
    )
    check("result has exactly 2 speakers", len(distinct(out)) == 2)
    check("input list not mutated", turns[2]["speaker"] == "SPEAKER_02")

    # Case 2: no embeddings -> no-op (can't measure similarity, so leave alone).
    out = merge_phantom_speakers(turns, None, max_seconds=3.0)
    check("None embeddings -> unchanged", len(distinct(out)) == 3)

    # Case 3: single speaker -> no-op.
    solo = [turn(0, 1, "SPEAKER_00")]
    out = merge_phantom_speakers(solo, {"SPEAKER_00": emb["SPEAKER_00"]}, max_seconds=3.0)
    check("single speaker -> unchanged", distinct(out) == ["SPEAKER_00"])

    # Case 4: a quiet-but-real speaker just ABOVE the threshold is kept
    # (conservative: we never absorb a genuine speaker).
    turns4 = [turn(0, 30, "SPEAKER_00"), turn(30, 34, "SPEAKER_01")]  # 01 = 4s
    out = merge_phantom_speakers(turns4, emb, max_seconds=3.0)
    check("quiet-but-real speaker (4s) kept", distinct(out) == ["SPEAKER_00", "SPEAKER_01"])

    # Case 5: two phantoms both fold into the real speaker, never into each other.
    emb5 = {
        "SPEAKER_00": np.array([1.0, 0.0, 0.0, 0.0], dtype="float32"),
        "SPEAKER_01": np.array([0.96, 0.04, 0.0, 0.0], dtype="float32"),  # phantom ~00
        "SPEAKER_02": np.array([0.90, 0.10, 0.0, 0.0], dtype="float32"),  # phantom ~00
    }
    turns5 = [
        turn(0, 60, "SPEAKER_00"),
        turn(60, 61, "SPEAKER_01"),  # 1s phantom
        turn(61, 62, "SPEAKER_02"),  # 1s phantom
    ]
    out = merge_phantom_speakers(turns5, emb5, max_seconds=3.0)
    check("two phantoms collapse onto the one real speaker", distinct(out) == ["SPEAKER_00"])

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("FAILED:", failed)
        return 1
    print("PASS - phantom-merge logic verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
