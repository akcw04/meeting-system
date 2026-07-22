"""Repair word-to-segment alignment for already-processed meetings.

Background
----------
WhisperX re-segments audio into its own (usually finer) pieces during forced
alignment, so its output does NOT line up 1:1 with Whisper's segments. The
original ``align_segments`` mapped aligned words back to segments *by position*
(``aligned_list[i] -> segments[i]``), which assigned each segment its
neighbour's words once the counts diverged. In the UI this showed up as a
transcript line's text "changing to something unrelated" the moment it became
the active (clicked) line, because the active row renders from the word-level
data instead of ``segment.text``.

``align.py`` now buckets words into segments by TIME. This script applies the
same correction to words already stored in the database: every word keeps its
(correct) timestamp and is re-assigned to the segment whose time span contains
it. No re-transcription or GPU work is required.

Usage
-----
    python tests/repair_word_alignment.py            # dry-run, all meetings
    python tests/repair_word_alignment.py --apply    # write the fix
    python tests/repair_word_alignment.py --apply --meeting 20 28
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"


def rebucket(segments: list[sqlite3.Row], words: list[sqlite3.Row]) -> dict[int, int]:
    """Return {word_id: correct_segment_id} using the same time-bucket rule as
    the fixed align.py. ``segments`` and ``words`` must be sorted by start time."""
    mapping: dict[int, int] = {}
    n = len(segments)
    si = 0
    for w in words:
        mid = (w["start_seconds"] + w["end_seconds"]) / 2.0
        while si < n and segments[si]["end_seconds"] <= mid:
            si += 1
        mapping[w["id"]] = segments[si if si < n else n - 1]["id"]
    return mapping


def repair(con: sqlite3.Connection, meeting_id: int, apply: bool) -> int:
    segs = con.execute(
        "SELECT id, start_seconds, end_seconds FROM segments WHERE meeting_id=? "
        "ORDER BY start_seconds, id", (meeting_id,)).fetchall()
    if not segs:
        return 0
    ids = [s["id"] for s in segs]
    placeholders = ",".join("?" * len(ids))
    words = con.execute(
        f"SELECT id, segment_id, start_seconds, end_seconds FROM words "
        f"WHERE segment_id IN ({placeholders}) ORDER BY start_seconds, id", ids).fetchall()
    if not words:
        print(f"  meeting {meeting_id}: no words")
        return 0
    mapping = rebucket(segs, words)
    return _finish(con, meeting_id, words, mapping, apply)


def _finish(con, meeting_id, words, mapping, apply) -> int:
    current = {w["id"]: w["segment_id"] for w in words}
    updates = [(seg_id, w_id) for w_id, seg_id in mapping.items() if current[w_id] != seg_id]
    verb = "moved" if apply else "would move"
    print(f"  meeting {meeting_id}: {len(words)} words, {verb} {len(updates)} "
          f"({100 * len(updates) / len(words):.0f}%)")
    if apply and updates:
        con.executemany("UPDATE words SET segment_id=? WHERE id=?", updates)
        con.commit()
    return len(updates)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--meeting", type=int, nargs="*", help="meeting id(s); default: all")
    args = ap.parse_args()

    if not DB_PATH.is_file():
        sys.exit(f"DB not found at {DB_PATH}")
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    if args.meeting:
        meeting_ids = args.meeting
    else:
        meeting_ids = [r["id"] for r in con.execute(
            "SELECT id FROM meetings ORDER BY id").fetchall()]

    mode = "APPLYING" if args.apply else "DRY-RUN (use --apply to write)"
    print(f"Word-alignment repair — {mode}")
    total = sum(repair(con, mid, args.apply) for mid in meeting_ids)
    print(f"Total words {'moved' if args.apply else 'to move'}: {total}")
    con.close()


if __name__ == "__main__":
    main()
