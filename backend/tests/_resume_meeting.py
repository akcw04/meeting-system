"""Resume a stuck meeting from any pipeline stage. No re-upload needed.

Usage:
    python tests\\_resume_meeting.py 20                # use meeting's stored language settings
    python tests\\_resume_meeting.py 20 --lang en      # force English transcription

Caches intermediate results (vad_segments.json, transcript.json) so that a
failure in alignment or diarization doesn't force re-running transcription.
Delete the cache files manually if you want to force a clean re-run.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from app.config import settings
from app.db import get_conn
from app.pipeline.align import align_segments
from app.pipeline.audio import run_vad
from app.pipeline.diarize import (
    assign_speakers_to_segments,
    diarize,
    merge_phantom_speakers,
)
from app.pipeline.transcribe import transcribe


def stamp(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def set_status(meeting_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE meetings SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, meeting_id),
        )


def main(meeting_id: int, lang_override: str | None = None) -> int:
    with get_conn() as conn:
        m = conn.execute(
            "SELECT id, audio_path, primary_language, expected_speakers, status "
            "FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    if m is None:
        print(f"ERROR: meeting {meeting_id} not found in DB")
        return 1

    audio_wav = Path(m["audio_path"])
    meeting_dir = audio_wav.parent
    vad_json = meeting_dir / "vad_segments.json"
    transcript_cache = meeting_dir / "_transcript_cache.json"

    if not audio_wav.is_file():
        print(f"ERROR: audio file missing at {audio_wav}")
        return 1

    print(f"Resuming meeting {meeting_id}, current status: {m['status']}")
    print(f"Audio: {audio_wav} ({audio_wav.stat().st_size / (1024*1024):.1f} MB)")
    print(f"Expected speakers: {m['expected_speakers']} (None = auto-detect)")

    overall_t0 = time.time()

    # === VAD (if missing) ===
    if not vad_json.exists():
        stamp("VAD starting (chunked)...")
        t = time.time()
        speech = run_vad(audio_wav)
        vad_json.write_text(
            json.dumps([{"start": s, "end": e} for s, e in speech], indent=2)
        )
        set_status(meeting_id, "vad_done")
        stamp(f"VAD done: {len(speech)} regions, {time.time() - t:.1f}s")
    else:
        stamp(f"VAD already done ({vad_json.name} exists), skipping")

    # === Transcribe (resume from cache if available) ===
    # Decide which language to feed Whisper:
    #   1. CLI --lang override (wins)
    #   2. The meeting's primary_language if not 'auto'
    #   3. None (Whisper auto-detects)
    if lang_override:
        lang_hint = lang_override
        stamp(f"Forcing transcription language to '{lang_hint}' (CLI override)")
    elif m["primary_language"] and m["primary_language"] != "auto":
        lang_hint = m["primary_language"]
        stamp(f"Using meeting's primary_language='{lang_hint}' as Whisper hint")
    else:
        lang_hint = None
        stamp("Whisper will auto-detect language")

    if transcript_cache.exists():
        stamp(f"Transcript cache found at {transcript_cache.name}, loading...")
        cached = json.loads(transcript_cache.read_text(encoding="utf-8"))
        segments = cached["segments"]
        detected_lang = cached["detected_language"]
        stamp(f"Loaded {len(segments)} segments from cache (lang={detected_lang})")
    else:
        stamp("Transcription starting (this is the slow stage)...")
        set_status(meeting_id, "transcribing")
        t = time.time()
        segments, detected_lang = transcribe(audio_wav, language=lang_hint)
        stamp(f"Transcription done: {len(segments)} segments, lang={detected_lang}, {time.time() - t:.1f}s")
        transcript_cache.write_text(
            json.dumps({"detected_language": detected_lang, "segments": segments}, indent=2),
            encoding="utf-8",
        )
        stamp(f"Cached transcript to {transcript_cache.name}")

    # === Align ===
    stamp("Word alignment starting...")
    set_status(meeting_id, "aligning")
    t = time.time()
    aligned = align_segments(audio_wav, segments, detected_lang)
    total_words = sum(len(s.get("words", [])) for s in aligned)
    stamp(f"Alignment done: {total_words} words timed, {time.time() - t:.1f}s")

    # === Diarize ===
    stamp("Speaker diarization starting...")
    set_status(meeting_id, "diarizing")
    t = time.time()
    turns, speaker_embeddings = diarize(audio_wav, expected_speakers=m["expected_speakers"])
    if m["expected_speakers"] is None:
        turns = merge_phantom_speakers(turns, speaker_embeddings, settings.phantom_max_seconds)
    final_segments = assign_speakers_to_segments(aligned, turns)
    distinct_speakers = sorted({s.get("speaker") for s in final_segments if s.get("speaker")})
    stamp(f"Diarization done: {len(distinct_speakers)} distinct speakers, {time.time() - t:.1f}s")

    # === Persist (clearing any partial leftovers from a prior failed run) ===
    stamp("Saving to DB...")
    with get_conn() as conn:
        conn.execute("DELETE FROM words WHERE segment_id IN (SELECT id FROM segments WHERE meeting_id = ?)", (meeting_id,))
        conn.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM speakers WHERE meeting_id = ?", (meeting_id,))
        # Re-diarizing replaces segments with new ids, so any previously
        # extracted insights now cite dead segment ids. Clear them; they must
        # be re-generated (POST /categorize) after the transcript is rebuilt.
        for _t in ("action_items", "decisions", "deadlines", "issues", "risks"):
            conn.execute(f"DELETE FROM {_t} WHERE meeting_id = ?", (meeting_id,))

        seen: list[str] = []
        for s in final_segments:
            lbl = s.get("speaker")
            if lbl is not None and lbl not in seen:
                seen.append(lbl)
        speaker_id_map: dict[str, int] = {}
        for idx, pyannote_label in enumerate(seen, start=1):
            cursor = conn.execute(
                "INSERT INTO speakers (meeting_id, label) VALUES (?, ?)",
                (meeting_id, f"Speaker {idx}"),
            )
            speaker_id_map[pyannote_label] = cursor.lastrowid

        for seg in final_segments:
            sp_id = speaker_id_map.get(seg.get("speaker"))
            cursor = conn.execute(
                "INSERT INTO segments "
                "(meeting_id, speaker_id, start_seconds, end_seconds, text, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    sp_id,
                    seg["start_seconds"],
                    seg["end_seconds"],
                    seg["text"],
                    seg["language"],
                ),
            )
            segment_id = cursor.lastrowid
            for w in seg.get("words", []):
                conn.execute(
                    "INSERT INTO words (segment_id, start_seconds, end_seconds, text, score) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        segment_id,
                        w["start_seconds"],
                        w["end_seconds"],
                        w["text"],
                        w.get("score"),
                    ),
                )
        conn.execute(
            "UPDATE meetings SET status = 'diarized', language = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (detected_lang, meeting_id),
        )

    stamp(f"DONE. Total time: {(time.time() - overall_t0)/60:.1f} min")
    print(f"\nMeeting {meeting_id} status is now 'diarized'.")
    print(f"Inspect via: GET /meetings/{meeting_id}/transcript")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Resume a stuck meeting from any pipeline stage")
    p.add_argument("meeting_id", type=int, nargs="?", default=20)
    p.add_argument(
        "--lang",
        default=None,
        help="Force transcription language (e.g. 'en', 'zh'). "
        "Overrides the meeting's primary_language. Use this if Whisper "
        "auto-detected the wrong language.",
    )
    args = p.parse_args()
    sys.exit(main(args.meeting_id, lang_override=args.lang))
