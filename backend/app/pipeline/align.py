"""WhisperX phoneme-level forced alignment.

Whisper's native timestamps are at the segment level (5-30s blocks) and drift
because Whisper processes audio in 30-second windows. For accurate word-by-word
timing - needed for speaker diarization alignment (Phase 5) and the UI audio
player (Phase 9) - we use WhisperX.

WhisperX loads a separate phoneme model (wav2vec 2.0). That model doesn't try
to understand WHAT was said - it only recognizes WHEN each sound happened.
WhisperX then snaps each word from Whisper's text output to its exact acoustic
position in the audio. Result: word boundaries accurate to ~50 ms.

Different languages need different wav2vec 2.0 models (English, Mandarin, etc.).
We cache the most recently loaded one and reload if the language changes.
"""
from __future__ import annotations

from pathlib import Path

import whisperx

from app.config import settings


_align_model = None
_align_metadata = None
_loaded_language: str | None = None

# WhisperX ships no default wav2vec2.0 alignment model for Bahasa Melayu, but
# it does ship one for Indonesian - and the two languages share essentially the
# same phoneme inventory and Latin orthography. Since alignment only asks WHEN
# each sound occurred (never WHAT it meant), the Indonesian acoustic model
# transfers to Malay cleanly and keeps word-level timestamps working for the
# audio player. Without this, Malay meetings would fall through to the
# no-word-timings path below. See DECISIONS.md 2026-08-07.
_ALIGN_MODEL_OVERRIDES = {
    "ms": "cahya/wav2vec2-large-xlsr-indonesian",
}


class UnsupportedAlignLanguage(Exception):
    """WhisperX has no default wav2vec2.0 alignment model for this language."""


def get_align_model(language: str):
    """Load (and cache) the WhisperX alignment model for a given language.

    Raises UnsupportedAlignLanguage if WhisperX has no default model for the
    language (e.g. 'mi', 'sw', 'haw') and we have no override for it, or if an
    overridden model cannot be fetched.
    """
    global _align_model, _align_metadata, _loaded_language
    if _align_model is None or _loaded_language != language:
        try:
            _align_model, _align_metadata = whisperx.load_align_model(
                language_code=language,
                device=settings.whisper_device,
                model_name=_ALIGN_MODEL_OVERRIDES.get(language),
            )
        except ValueError as exc:
            raise UnsupportedAlignLanguage(language) from exc
        _loaded_language = language
    return _align_model, _align_metadata


def align_segments(
    audio_path: Path,
    segments: list[dict],
    language: str,
) -> list[dict]:
    """Add word-level timestamps to Whisper segments.

    Input segments shape:
        [{start_seconds, end_seconds, text, language}, ...]

    Output segments shape (same plus words):
        [{start_seconds, end_seconds, text, language,
          words: [{text, start_seconds, end_seconds, score}]}, ...]
    """
    if not segments:
        return []

    try:
        model, metadata = get_align_model(language)
    except UnsupportedAlignLanguage:
        # No word-level model for this language. Return the segments unchanged
        # (each gets an empty words list). The rest of the pipeline still runs.
        print(
            f"[align] WhisperX has no default alignment model for '{language}'. "
            f"Skipping word-level alignment; segments will have no word timings."
        )
        return [{**s, "words": []} for s in segments]

    # WhisperX uses its own key names internally
    whisperx_segments = [
        {"start": s["start_seconds"], "end": s["end_seconds"], "text": s["text"]}
        for s in segments
    ]

    audio = whisperx.load_audio(str(audio_path))

    aligned = whisperx.align(
        whisperx_segments,
        model,
        metadata,
        audio,
        device=settings.whisper_device,
        return_char_alignments=False,
    )

    # WhisperX does NOT return one aligned segment per input segment — it
    # re-segments freely (typically splitting Whisper's blocks into finer
    # sentence-level pieces, sometimes dropping/merging). So aligned_list does
    # not line up 1:1 with our input segments, and mapping by POSITION assigns
    # each segment its neighbour's words (the "line text changes when you click
    # it" bug). Instead: flatten every aligned word (each carries an absolute
    # timestamp) and bucket it into the original segment whose time span it
    # falls in. Robust to splits, merges and drops.
    aligned_list = aligned.get("segments", [])

    all_words: list[dict] = []
    for aseg in aligned_list:
        for w in aseg.get("words", []):
            # Punctuation and out-of-vocabulary tokens may lack start/end times
            if "start" not in w or "end" not in w:
                continue
            score = w.get("score")
            all_words.append(
                {
                    "text": w["word"],
                    "start_seconds": float(w["start"]),
                    "end_seconds": float(w["end"]),
                    "score": float(score) if score is not None else None,
                }
            )
    all_words.sort(key=lambda w: (w["start_seconds"], w["end_seconds"]))

    out: list[dict] = [{**s, "words": []} for s in segments]
    n = len(segments)
    si = 0
    for w in all_words:
        mid = (w["start_seconds"] + w["end_seconds"]) / 2.0
        # segments are time-ordered; advance to the first whose end is past
        # this word's midpoint (words are sorted, so si only moves forward).
        while si < n and segments[si]["end_seconds"] <= mid:
            si += 1
        out[si if si < n else n - 1]["words"].append(w)
    return out
