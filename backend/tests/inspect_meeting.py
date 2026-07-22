"""Print a readable inspection of any meeting that's been processed.

Usage:
    python tests\\inspect_meeting.py             # defaults to meeting 20
    python tests\\inspect_meeting.py 20          # specific meeting
    python tests\\inspect_meeting.py 20 --full   # dump the entire transcript
    python tests\\inspect_meeting.py 20 --samples 30   # show 30 sample segments

By default shows: meeting metadata, per-speaker breakdown (word count,
talk time, share %), and 20 sample segments (first 15 + last 5).
"""
import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from app.db import get_conn


def fmt_time(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:d}:{sec:02d}"


def main(meeting_id: int, full: bool, sample_count: int) -> int:
    with get_conn() as conn:
        m = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if m is None:
            print(f"Meeting {meeting_id} not found.")
            return 1

        speakers = conn.execute(
            "SELECT * FROM speakers WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()

        segments = conn.execute(
            "SELECT * FROM segments WHERE meeting_id = ? ORDER BY start_seconds",
            (meeting_id,),
        ).fetchall()

        word_counts = conn.execute(
            "SELECT segment_id, COUNT(*) AS n "
            "FROM words WHERE segment_id IN "
            "(SELECT id FROM segments WHERE meeting_id = ?) "
            "GROUP BY segment_id",
            (meeting_id,),
        ).fetchall()
        words_by_segment = {row["segment_id"]: row["n"] for row in word_counts}

    spk_label = {s["id"]: (s["display_name"] or s["label"]) for s in speakers}

    print("=" * 70)
    print(f"MEETING {meeting_id}: {m['title']}")
    print("=" * 70)
    print(f"  File              : {m['original_filename']}")
    print(f"  Duration          : {fmt_time(m['duration_seconds'] or 0)}  ({m['duration_seconds']:.1f}s)")
    print(f"  Detected language : {m['language']}")
    print(f"  Primary language  : {m['primary_language']}")
    print(f"  Expected speakers : {m['expected_speakers']}")
    print(f"  Status            : {m['status']}")
    print(f"  Created           : {m['created_at']}")

    print(f"\n  Segments          : {len(segments)}")
    print(f"  Words (timed)     : {sum(words_by_segment.values())}")
    print(f"  Speakers          : {len(speakers)}")

    # Per-speaker breakdown
    print("\n" + "-" * 70)
    print("SPEAKER BREAKDOWN")
    print("-" * 70)
    print(f"  {'Speaker':<20} {'Segments':>8}  {'Talk time':>10}  {'Share':>6}  {'Words':>8}")
    by_speaker: dict[int | None, dict] = {}
    for seg in segments:
        sid = seg["speaker_id"]
        bucket = by_speaker.setdefault(
            sid,
            {"segments": 0, "talk_time": 0.0, "words": 0},
        )
        bucket["segments"] += 1
        bucket["talk_time"] += seg["end_seconds"] - seg["start_seconds"]
        bucket["words"] += words_by_segment.get(seg["id"], 0)

    total_talk = sum(b["talk_time"] for b in by_speaker.values()) or 1.0
    for sid, b in sorted(
        by_speaker.items(),
        key=lambda kv: -kv[1]["talk_time"],
    ):
        label = spk_label.get(sid, "Unknown") if sid is not None else "(no speaker)"
        share = (b["talk_time"] / total_talk) * 100
        print(
            f"  {label:<20} {b['segments']:>8}  {fmt_time(b['talk_time']):>10}  "
            f"{share:>5.1f}%  {b['words']:>8}"
        )

    # Sample segments
    print("\n" + "-" * 70)
    print("TRANSCRIPT" + ("  (full)" if full else f"  (sample of {sample_count})"))
    print("-" * 70)

    if full:
        to_show = segments
    else:
        first_n = min(sample_count - 5, len(segments))
        first = segments[: max(first_n, 0)]
        last = segments[-5:] if len(segments) > first_n else []
        to_show = first + ([None] if last and last[0] != first[-1] if first else []) + last

    for seg in to_show:
        if seg is None:
            print(f"\n  ... ({len(segments) - sample_count} segments hidden) ...\n")
            continue
        who = spk_label.get(seg["speaker_id"], "unknown")
        ts = f"[{fmt_time(seg['start_seconds'])}–{fmt_time(seg['end_seconds'])}]"
        text = seg["text"].strip()
        if len(text) > 200:
            text = text[:200] + "..."
        print(f"  {ts:<22} {who:<12} {text}")

    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Inspect a processed meeting")
    p.add_argument("meeting_id", type=int, nargs="?", default=20)
    p.add_argument("--full", action="store_true", help="Print every segment")
    p.add_argument("--samples", type=int, default=20, help="How many sample segments to show (default 20)")
    args = p.parse_args()
    sys.exit(main(args.meeting_id, args.full, args.samples))
