"""Pipeline orchestrator - the single entrypoint called by FastAPI's
BackgroundTasks after an upload finishes.

Stages and the status values they leave behind:
    uploaded            -> the route's initial state
    audio_extracted     -> FFmpeg done, WAV on disk, duration known
    transcribing        -> Whisper running (Silero VAD pre-filtering is built
                           into Faster-Whisper's vad_filter; no separate stage)
    transcribed         -> Whisper segments produced (in memory)
    aligning            -> WhisperX phoneme alignment running
    aligned             -> word-level timestamps attached (in memory)
    diarizing           -> pyannote.audio running
    diarized            -> speakers, segments, words all persisted to DB
    categorizing        -> Llama 3.1 extracting insights (Phase 7)
    ready               -> insights persisted; meeting fully processed
    categorize_failed: <reason>
                        -> transcript IS usable, but the LLM step failed
                           (e.g. Ollama not running). Retry-able.
    error: <reason>     -> an earlier stage blew up; truncated message stored
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.db import get_conn
from app.pipeline.align import align_segments
from app.pipeline.audio import extract_audio_to_wav
from app.pipeline.categorize import categorize_transcript
from app.pipeline.diarize import (
    assign_speakers_to_segments,
    diarize,
    merge_phantom_speakers,
)
from app.pipeline.transcribe import (
    SUPPORTED_LANGUAGES,
    detect_languages,
    languages_in_segments,
    transcribe,
    transcribe_codeswitch,
)

# Only one meeting may occupy the GPU pipeline at a time. FastAPI background
# tasks run in a threadpool; this RLock serializes them so concurrent uploads
# (or an upload plus a manual categorize) can't oversubscribe the 6 GB card or
# race on the shared model singletons. RLock (not Lock) lets run_pipeline ->
# categorize_safe re-enter on the SAME thread without deadlocking.
_PIPELINE_LOCK = threading.RLock()


def _set_status(meeting_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE meetings SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, meeting_id),
        )


def _set_progress(meeting_id: int, fraction: float) -> None:
    """Store live progress (0..1) for the current long stage."""
    frac = max(0.0, min(1.0, fraction))
    with get_conn() as conn:
        conn.execute(
            "UPDATE meetings SET progress = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (frac, meeting_id),
        )


def _throttled_progress(meeting_id: int, total_seconds: float, every: float = 2.0):
    """Per-segment callback(seconds) that writes progress at most once per
    `every` seconds - avoids hammering the DB during transcription."""
    last = [0.0]

    def cb(current_seconds: float) -> None:
        now = time.monotonic()
        if total_seconds and now - last[0] >= every:
            last[0] = now
            _set_progress(meeting_id, current_seconds / total_seconds)

    return cb


def run_pipeline(meeting_id: int, original_path: Path) -> None:
    """End-to-end pipeline up to (and including) speaker diarization."""
    original_path = Path(original_path)
    meeting_dir = original_path.parent
    audio_wav = meeting_dir / "audio.wav"

    # Serialize the GPU pipeline: a second upload's task waits here until the
    # current one finishes (RLock re-entered by the in-pipeline categorize).
    _PIPELINE_LOCK.acquire()
    try:
        # === Stage A: audio extraction ===
        duration = extract_audio_to_wav(original_path, audio_wav)
        with get_conn() as conn:
            conn.execute(
                "UPDATE meetings SET duration_seconds = ?, audio_path = ?, "
                "status = 'audio_extracted', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (duration, str(audio_wav), meeting_id),
            )

        # (Former Stage B - standalone Silero VAD - removed 2026-06-14. Silero
        # VAD now runs INSIDE Faster-Whisper via vad_filter, pre-filtering
        # non-speech before transcription; the old separate pass wrote an unused
        # vad_segments.json sidecar. This IS the IR's Silero VAD, integrated into
        # the ASR engine - see DECISIONS.md.)

        # === Stage C: transcription (Whisper) ===
        with get_conn() as conn:
            row = conn.execute(
                "SELECT primary_language FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
        primary = (row["primary_language"] if row else "auto") or "auto"

        # Detect the spoken languages up front so we can spot code-switched
        # recordings - English, Mandarin and Bahasa Melayu in any combination,
        # the Malaysian meeting case. Cheap: only a few short windows are
        # sampled, not the whole file.
        detected = detect_languages(audio_wav)
        langs_present = sorted(set(detected))
        dominant = max(langs_present, key=detected.count) if detected else "en"
        is_mixed = len(langs_present) >= 2

        # The user's preferred language for the OUTPUT (summary, insights, Word
        # doc); 'auto' falls back to the dominant detected language. The
        # TRANSCRIPT itself is always kept as-spoken (see below).
        preferred = primary if primary in SUPPORTED_LANGUAGES else dominant

        _set_status(meeting_id, "transcribing")
        _set_progress(meeting_id, 0.0)
        # Transcript policy (✋ DECISIONS 2026-06-21): show the ORIGINAL as-spoken
        # words so users can verify the capture (trust). A code-switched recording
        # is transcribed per-language (each part stays in its own language); a
        # single-language recording uses the fast one-pass path. The single-
        # language OUTPUT (summary / insights / Word doc) is produced downstream by
        # the LLM, which reads the mixed transcript and writes in `preferred`.
        if is_mixed:
            segments, detected_lang = transcribe_codeswitch(
                audio_wav,
                dominant=dominant,
                progress_callback=_throttled_progress(meeting_id, duration),
            )
        else:
            segments, detected_lang = transcribe(
                audio_wav,
                language=preferred,
                task="transcribe",
                progress_callback=_throttled_progress(meeting_id, duration),
            )

        # Record the languages the transcript ACTUALLY turned out to contain.
        # Written here, after transcription, rather than from the pre-scan above:
        # the pre-scan judges one language per 25-second window, so a language
        # spoken only in short bursts is outvoted and never recorded. That
        # under-reported a real trilingual test recording as bilingual even
        # though its Mandarin had been transcribed correctly. Drives the UI's
        # mixed-language notice. Stored as e.g. "en,ms" or "en,ms,zh".
        with get_conn() as conn:
            conn.execute(
                "UPDATE meetings SET languages_detected = ? WHERE id = ?",
                (",".join(languages_in_segments(segments)), meeting_id),
            )

        # === Stage D: word-level alignment (WhisperX) ===
        _set_status(meeting_id, "aligning")
        aligned_segments = align_segments(audio_wav, segments, detected_lang)

        # === Stage E: speaker diarization (pyannote) ===
        # Read the meeting's expected_speakers hint (NULL = auto).
        with get_conn() as conn:
            row = conn.execute(
                "SELECT expected_speakers FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
        expected = row["expected_speakers"] if row else None

        _set_status(meeting_id, "diarizing")
        turns, speaker_embeddings = diarize(audio_wav, expected_speakers=expected)
        # When the user didn't say how many speakers to expect, pyannote can
        # over-segment. Fold phantom speakers (< phantom_max_seconds of total
        # talk-time) into their nearest voiceprint. Skipped when a count was
        # given - that already forced pyannote to exactly that many clusters.
        if expected is None:
            before = len({t["speaker"] for t in turns})
            turns = merge_phantom_speakers(
                turns, speaker_embeddings, settings.phantom_max_seconds
            )
            after = len({t["speaker"] for t in turns})
            if after != before:
                print(
                    f"[diarize] phantom-merge: {before} -> {after} speakers "
                    f"(< {settings.phantom_max_seconds}s talk-time)"
                )
        final_segments = assign_speakers_to_segments(aligned_segments, turns)

        # === Persist speakers, segments, words ===
        with get_conn() as conn:
            # Unique speaker labels in the order they appear in the audio
            seen: list[str] = []
            for s in final_segments:
                lbl = s.get("speaker")
                if lbl is not None and lbl not in seen:
                    seen.append(lbl)

            # Insert speakers as "Speaker 1", "Speaker 2", ... and remember the mapping
            speaker_id_map: dict[str, int] = {}
            for idx, pyannote_label in enumerate(seen, start=1):
                cursor = conn.execute(
                    "INSERT INTO speakers (meeting_id, label) VALUES (?, ?)",
                    (meeting_id, f"Speaker {idx}"),
                )
                speaker_id_map[pyannote_label] = cursor.lastrowid

            # Insert segments (with speaker_id) and their words
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

        # === Stage F: LLM categorization (Phase 7) ===
        # Failure here must NOT mark the whole meeting as failed - the
        # transcript is already persisted and usable. categorize_safe
        # handles its own error capture + 'categorize_failed:' status.
        categorize_safe(meeting_id)
        return

    except Exception as exc:
        import traceback as _tb

        # Short error for the API/UI (visible in meetings.status).
        err = f"error: {type(exc).__name__}: {str(exc)[:400]}"

        # Full traceback in a sidecar file next to the audio for debugging.
        try:
            (meeting_dir / "error_trace.txt").write_text(
                f"{type(exc).__name__}: {exc}\n\n{_tb.format_exc()}",
                encoding="utf-8",
            )
        except Exception:
            pass

        try:
            _set_status(meeting_id, err)
        except Exception:
            pass
        raise
    finally:
        _PIPELINE_LOCK.release()


def categorize_safe(meeting_id: int) -> None:
    """run_categorization with error capture.

    On failure: writes the full traceback to the meeting's upload folder
    and sets status to 'categorize_failed: <reason>' (retry-able via
    POST /meetings/{id}/categorize). Never raises.
    """
    _PIPELINE_LOCK.acquire()
    try:
        run_categorization(meeting_id)
    except Exception as exc:
        import traceback as _tb

        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT audio_path FROM meetings WHERE id = ?", (meeting_id,)
                ).fetchone()
            if row and row["audio_path"]:
                trace_path = Path(row["audio_path"]).parent / "error_trace.txt"
                trace_path.write_text(
                    f"{type(exc).__name__}: {exc}\n\n{_tb.format_exc()}",
                    encoding="utf-8",
                )
        except Exception:
            pass
        try:
            _set_status(
                meeting_id,
                f"categorize_failed: {type(exc).__name__}: {str(exc)[:300]}",
            )
        except Exception:
            pass
    finally:
        _PIPELINE_LOCK.release()


def run_categorization(meeting_id: int) -> None:
    """Stage F: extract insights with Llama 3.1 and persist them.

    Standalone so it can also be invoked as a retry (e.g. after a
    'categorize_failed' status, or to re-categorize older meetings that
    finished before Phase 7 existed).
    """
    # Llama 3.1 8B needs ~5 GB VRAM in Ollama's process. Hand back
    # everything our torch models are holding first (Phase 6).
    from app.gpu import free_all_torch_models, report_vram

    free_all_torch_models()
    print(report_vram("before Ollama (after free_all)"))

    with get_conn() as conn:
        meeting = conn.execute(
            "SELECT primary_language, language, created_at FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            raise ValueError(f"Meeting {meeting_id} not found")
        seg_rows = conn.execute(
            "SELECT id, speaker_id, text FROM segments WHERE meeting_id = ? "
            "ORDER BY start_seconds",
            (meeting_id,),
        ).fetchall()
        spk_rows = conn.execute(
            "SELECT id, label, display_name FROM speakers WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchall()

    if not seg_rows:
        raise ValueError(f"Meeting {meeting_id} has no transcript segments")

    segments = [dict(r) for r in seg_rows]
    speaker_labels = {r["id"]: (r["display_name"] or r["label"]) for r in spk_rows}

    # Output language: the user's choice, falling back to what was detected.
    # The transcript is kept as-spoken (possibly mixed); the LLM reads it and
    # writes the summary + insights in this single output language.
    primary = (meeting["primary_language"] or "auto").lower()
    out_lang = primary if primary != "auto" else (meeting["language"] or "en")

    _set_status(meeting_id, "categorizing")
    _set_progress(meeting_id, 0.0)
    # Anchor for turning spoken date cues into calendar dates ("by Friday" ->
    # "by Friday (15/08/2026)"). The upload timestamp stands in for the meeting
    # date, which is exact when a recording is uploaded the day it was made and
    # wrong by the delay when it is not - hence the resolved date is shown as an
    # editable suggestion beside the words actually spoken, never in place of them.
    meeting_date = None
    raw_created = meeting["created_at"] if "created_at" in meeting.keys() else None
    if raw_created:
        try:
            meeting_date = datetime.fromisoformat(str(raw_created).replace("Z", "")).date()
        except ValueError:
            meeting_date = None

    insights = categorize_transcript(
        segments,
        speaker_labels,
        out_lang,
        on_progress=lambda f: _set_progress(meeting_id, f),
        meeting_date=meeting_date,
    )

    def first_or_none(ids: list[int]) -> int | None:
        return ids[0] if ids else None

    with get_conn() as conn:
        # Idempotent: clear any previous insights for this meeting first.
        for table in ("action_items", "decisions", "deadlines", "issues", "risks"):
            conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))

        for it in insights.action_items:
            conn.execute(
                "INSERT INTO action_items (meeting_id, description, owner, due_date, source_segment_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (meeting_id, it.description, it.owner, it.due, first_or_none(it.source_segment_ids)),
            )
        for it in insights.decisions:
            conn.execute(
                "INSERT INTO decisions (meeting_id, description, source_segment_id) VALUES (?, ?, ?)",
                (meeting_id, it.description, first_or_none(it.source_segment_ids)),
            )
        for it in insights.deadlines:
            conn.execute(
                "INSERT INTO deadlines (meeting_id, description, target_date, source_segment_id) "
                "VALUES (?, ?, ?, ?)",
                (meeting_id, it.description, it.date, first_or_none(it.source_segment_ids)),
            )
        for it in insights.issues:
            conn.execute(
                "INSERT INTO issues (meeting_id, description, source_segment_id) VALUES (?, ?, ?)",
                (meeting_id, it.description, first_or_none(it.source_segment_ids)),
            )
        for it in insights.risks:
            conn.execute(
                "INSERT INTO risks (meeting_id, description, mitigation, source_segment_id) "
                "VALUES (?, ?, ?, ?)",
                (meeting_id, it.description, it.mitigation, first_or_none(it.source_segment_ids)),
            )
        conn.execute(
            "UPDATE meetings SET summary = ?, status = 'ready', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (insights.summary, meeting_id),
        )
