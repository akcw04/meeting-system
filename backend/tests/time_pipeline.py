"""Timed pipeline run for Table 5.9 (processing-time evaluation).

Re-runs the real pipeline stages on an ALREADY-PROCESSED meeting's audio and
prints wall-clock times in exactly the shape Table 5.9 wants:

    audio length | transcription-to-diarization | categorization | total

Nothing is written to the database - the meeting's stored transcript and
insights are untouched. The stages run with the same routing the live
pipeline uses (language detection, code-switch handling, expected speakers,
GPU freeing before the LLM).

USAGE (from backend/, venv active; Ollama must be running):
    python tests\\time_pipeline.py <meeting_id>

Run each Table 5.8 meeting in turn, e.g.:
    python tests\\time_pipeline.py 20
    python tests\\time_pipeline.py 27
    python tests\\time_pipeline.py 28
    python tests\\time_pipeline.py 31

NOTE: run this in YOUR OWN terminal and let it finish - a long Mandarin
meeting's categorization alone can take well over an hour on the iGPU.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.pipeline.align import align_segments  # noqa: E402
from app.pipeline.categorize import categorize_transcript  # noqa: E402
from app.pipeline.diarize import (  # noqa: E402
    assign_speakers_to_segments,
    diarize,
    merge_phantom_speakers,
)
from app.pipeline.transcribe import (  # noqa: E402
    detect_languages,
    transcribe,
    transcribe_codeswitch,
)


def fmt(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(1)
    meeting_id = int(sys.argv[1])

    with get_conn() as conn:
        row = conn.execute(
            "SELECT audio_path, duration_seconds, primary_language, expected_speakers "
            "FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    if row is None or not row["audio_path"]:
        raise SystemExit(f"Meeting {meeting_id} not found or has no audio.")
    audio = Path(row["audio_path"])
    if not audio.exists():
        raise SystemExit(f"Audio file missing on disk: {audio}")
    duration = float(row["duration_seconds"] or 0)
    primary = (row["primary_language"] or "auto") or "auto"
    expected = row["expected_speakers"]

    print(f"Meeting {meeting_id}: {audio}")
    print(f"Audio length: {fmt(duration)} ({duration/60:.1f} min)")
    print("=" * 60)

    # --- transcription (same routing as the live pipeline) ---
    t0 = time.monotonic()
    detected = detect_languages(audio)
    langs = sorted(set(detected))
    dominant = max(langs, key=detected.count) if detected else "en"
    is_mixed = len(langs) >= 2
    preferred = primary if primary in ("en", "zh") else dominant
    if is_mixed:
        segments, lang = transcribe_codeswitch(audio, dominant=dominant)
    else:
        segments, lang = transcribe(audio, language=preferred, task="transcribe")
    t_transcribe = time.monotonic() - t0
    print(f"transcription ({'code-switch' if is_mixed else lang}): {fmt(t_transcribe)}")

    # --- alignment ---
    t0 = time.monotonic()
    aligned = align_segments(audio, segments, lang)
    t_align = time.monotonic() - t0
    print(f"alignment: {fmt(t_align)}")

    # --- diarization (+ phantom merge, as live) ---
    t0 = time.monotonic()
    turns, embeddings = diarize(audio, expected_speakers=expected)
    if expected is None:
        turns = merge_phantom_speakers(turns, embeddings, settings.phantom_max_seconds)
    final_segments = assign_speakers_to_segments(aligned, turns)
    t_diarize = time.monotonic() - t0
    print(f"diarization: {fmt(t_diarize)}")

    # --- categorization: timed on the meeting's STORED segments, NOT the fresh
    # transcription above. The batched fresh pass produces much coarser
    # segmentation than the stored data (e.g. mtg 27: 375 fresh vs 2131 stored),
    # which changes the chunk count and the LLM workload. Timing categorization
    # on the real stored transcript makes this row consistent with the stored
    # insights and with the accuracy table (bug #32). DB is still untouched -
    # categorize_transcript only reads. ---
    from app.gpu import free_all_torch_models

    free_all_torch_models()
    with get_conn() as conn:
        seg_rows = conn.execute(
            "SELECT id, speaker_id, text FROM segments WHERE meeting_id = ? "
            "ORDER BY start_seconds",
            (meeting_id,),
        ).fetchall()
        spk_rows = conn.execute(
            "SELECT id, label, display_name FROM speakers WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchall()
    stored_segments = [dict(r) for r in seg_rows]
    speaker_labels = {r["id"]: (r["display_name"] or r["label"]) for r in spk_rows}
    print(f"categorizing {len(stored_segments)} stored segments "
          f"(fresh pass produced {len(final_segments)})...")
    out_lang = primary if primary != "auto" else lang
    t0 = time.monotonic()
    categorize_transcript(stored_segments, speaker_labels, out_lang)
    t_cat = time.monotonic() - t0
    print(f"categorization: {fmt(t_cat)}")

    # --- Table 5.9 summary ---
    t_front = t_transcribe + t_align + t_diarize
    total = t_front + t_cat
    target_low = duration / 60 * (5 / 60)   # 5 min per hour of audio
    target_high = duration / 60 * (10 / 60)  # 10 min per hour of audio
    print("\n" + "=" * 60)
    print(f"TABLE 5.9 ROW - meeting {meeting_id}")
    print("=" * 60)
    print(f"  Audio length                : {fmt(duration)}")
    print(f"  Transcription to diarization: {fmt(t_front)}")
    print(f"  Categorization              : {fmt(t_cat)}")
    print(f"  Total                       : {fmt(total)}")
    print(f"  Ch3 target for this length  : {fmt(target_low*60)} - {fmt(target_high*60)}")
    print(f"  Front pipeline within target: {'YES' if t_front <= target_high*60 else 'NO'}")
    print(f"  Total within target         : {'YES' if total <= target_high*60 else 'NO'}")


if __name__ == "__main__":
    main()
