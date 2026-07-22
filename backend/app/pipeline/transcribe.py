"""Faster-Whisper transcription - pure function, no DB writes.

First call downloads the model into `data/models/` (~480 MB for 'small',
~3 GB for 'large-v3'). Subsequent calls reuse the cached weights.

By default we auto-detect the language, but the caller can force one via the
`language` argument. The runner passes the user's `primary_language` when it
is 'en'/'zh' (added after bug #13, where auto-detect misfired to Maori on a
long English meeting). 'auto' still lets Whisper detect per audio.
"""
from __future__ import annotations

from pathlib import Path

import soundfile as sf
from faster_whisper import BatchedInferencePipeline, WhisperModel

from app.config import settings


_whisper_model: WhisperModel | None = None
_batched_pipeline: BatchedInferencePipeline | None = None
_loaded_name: str | None = None


def get_whisper_model() -> WhisperModel:
    """Load (and cache) the Faster-Whisper model named by settings."""
    global _whisper_model, _batched_pipeline, _loaded_name
    name = settings.whisper_model
    if _whisper_model is None or _loaded_name != name:
        settings.ensure_dirs()
        _whisper_model = WhisperModel(
            name,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            download_root=str(settings.models_dir),
        )
        # Batched pipeline runs multiple VAD windows in parallel - ~3-4x faster
        # on long audio with the same model + accuracy. Used when
        # whisper_batch_size > 1; lower the batch size if VRAM is tight.
        _batched_pipeline = BatchedInferencePipeline(model=_whisper_model)
        _loaded_name = name
    return _whisper_model


def detect_languages(
    audio_path: Path,
    windows: int = 10,
    window_seconds: float = 25.0,
    min_prob: float = 0.3,
    candidates: tuple[str, ...] = ("en", "zh"),
) -> list[str]:
    """Sample windows across the audio and detect each window's spoken language.

    Used to spot code-switched recordings (the IR's English-Mandarin headline)
    BEFORE transcription, so the runner can route them (e.g. translate a mixed
    meeting to a single output language). Reads only the sampled windows (not
    the whole file) so it stays cheap on multi-hour recordings.

    Returns the per-window detected languages that cleared `min_prob` and are in
    `candidates` - duplicates kept, so the caller can read both the SET of
    languages present (mixed?) and the DOMINANT one (most frequent).
    """
    model = get_whisper_model()
    found: list[str] = []
    try:
        with sf.SoundFile(str(audio_path)) as f:
            sr = f.samplerate
            total = len(f) / sr if sr else 0.0
            n = max(1, windows)
            win = int(window_seconds * sr)
            for i in range(n):
                start = (i + 0.5) * total / n - window_seconds / 2
                start = max(0.0, min(start, max(0.0, total - window_seconds)))
                f.seek(int(start * sr))
                chunk = f.read(win, dtype="float32")
                if getattr(chunk, "ndim", 1) > 1:
                    chunk = chunk.mean(axis=1)
                if len(chunk) < sr * 2:  # too short to judge
                    continue
                try:
                    res = model.detect_language(chunk)
                except Exception:
                    continue
                lang = res[0] if isinstance(res, (tuple, list)) else res
                prob = res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else 1.0
                if lang in candidates and (prob is None or prob >= min_prob):
                    found.append(lang)
    except Exception as exc:  # detection is best-effort; never block the pipeline
        print(f"[detect_languages] skipped ({type(exc).__name__}: {exc})")
    return found


def transcribe(
    audio_path: Path,
    language: str | None = None,
    task: str = "transcribe",
    progress_callback=None,
    allow_batched: bool = True,
) -> tuple[list[dict], str]:
    """Transcribe a 16 kHz mono WAV.

    Args:
        audio_path: path to the audio file.
        language: ISO language code ('en', 'zh', etc.) to force the
            transcription language. None = let Whisper auto-detect.
        task: 'transcribe' (verbatim, in the spoken language) or 'translate'
            (Whisper translates any spoken language TO ENGLISH in one pass -
            used to turn a code-switched meeting into a single-language English
            transcript). 'translate' only ever outputs English.
        progress_callback: optional callable(seconds: float) invoked once per
            segment with that segment's end time, for live progress reporting.

    Returns:
        (segments, output_language)
        segments is a list of dicts with: start_seconds, end_seconds, text, language
        output_language is the language of the produced text: 'en' when
        task='translate', otherwise the forced/auto-detected language.
    """
    model = get_whisper_model()
    # Silero VAD pre-filtering (the IR's VAD step) is integrated into the ASR
    # engine: it drops non-speech before transcription, cutting silence-induced
    # hallucinations and GPU compute. Threshold is tunable for evaluation.
    opts = dict(
        language=language,     # None = auto-detect, else forced
        task=task,             # 'transcribe' or 'translate' (-> English)
        beam_size=5,
        vad_filter=True,
        vad_parameters={"threshold": settings.whisper_vad_threshold},
        word_timestamps=False, # word-level timing comes in Phase 4 via WhisperX
    )
    batch = settings.whisper_batch_size
    # The batched pipeline mishandles task="translate": it leaves the source
    # language in (Mandarin parts stay Mandarin) and emits very coarse segments
    # that merge speaker turns - verified on meeting 28. So translation always
    # goes through the sequential path (clean English + fine segments); batched
    # is only for same-language transcription, where it's a clean ~3-4x speed-up.
    use_batched = (
        allow_batched and bool(batch) and batch > 1
        and _batched_pipeline is not None and task != "translate"
    )
    if use_batched:
        try:
            segments_iter, info = _batched_pipeline.transcribe(
                str(audio_path), batch_size=batch, **opts
            )
        except TypeError:
            # Defensive: if this faster-whisper's batched API differs, fall
            # back to sequential transcription (slower but always works).
            segments_iter, info = model.transcribe(str(audio_path), **opts)
    else:
        segments_iter, info = model.transcribe(str(audio_path), **opts)

    # When translating, the produced TEXT is English regardless of what was
    # spoken - so the output language (used for alignment + stored per segment)
    # is 'en', not the source language Whisper detected.
    output_lang = "en" if task == "translate" else info.language

    out: list[dict] = []
    for seg in segments_iter:
        out.append(
            {
                "start_seconds": float(seg.start),
                "end_seconds": float(seg.end),
                "text": seg.text.strip(),
                "language": output_lang,
            }
        )
        if progress_callback is not None:
            progress_callback(float(seg.end))
    return out, output_lang


def transcribe_codeswitch(
    audio_path: Path,
    dominant: str,
    candidates: tuple[str, ...] = ("en", "zh"),
    progress_callback=None,
) -> tuple[list[dict], str]:
    """Accurate transcription of a code-switched (mixed-language) recording.

    Each part stays in the language it was spoken (NO translation), so the
    displayed transcript faithfully shows what was captured - which is what lets
    users verify accuracy and trust the system (DECISIONS 2026-06-21).

    Strategy - TWO FULL passes (not short per-segment clips, which lose context
    and mis-hear, e.g. "我" -> "宝宝"):
      1. the DOMINANT language over the whole file -> defines the segmentation;
      2. the OTHER language over the whole file -> full-context text for the
         minority-language parts.
    Each segment is language-detected on its audio, but only flipped on a
    CONFIDENT result (so a dominant-language line isn't turned into the wrong
    language). Minority segments take their text from the second pass, matched by
    time overlap. Returns (segments, dominant_language).
    """
    model = get_whisper_model()
    other = next((c for c in candidates if c != dominant), None)
    # Pass 1: whole file in the dominant language (sequential, not batched - the
    # batched pipeline merges turns into very coarse segments for some audio).
    # This pass defines the segmentation we keep.
    segments, _ = transcribe(
        audio_path, language=dominant, task="transcribe",
        allow_batched=False, progress_callback=progress_callback,
    )
    if not other or not segments:
        return segments, dominant

    # Which segments are actually the OTHER language? Detect on each segment's
    # audio; only flip on a confident result so dominant lines aren't mislabelled.
    minority: list[int] = []
    try:
        with sf.SoundFile(str(audio_path)) as f:
            sr = f.samplerate
            for i, seg in enumerate(segments):
                dur = seg["end_seconds"] - seg["start_seconds"]
                if dur < 0.6:  # too short to judge reliably
                    continue
                f.seek(int(seg["start_seconds"] * sr))
                chunk = f.read(int(dur * sr), dtype="float32")
                if getattr(chunk, "ndim", 1) > 1:
                    chunk = chunk.mean(axis=1)
                try:
                    res = model.detect_language(chunk)
                except Exception:
                    continue
                lang = res[0] if isinstance(res, (tuple, list)) else res
                prob = res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else 1.0
                if lang == other and (prob is None or prob >= 0.55):
                    minority.append(i)
    except Exception as exc:  # best-effort; fall back to the dominant-only pass
        print(f"[codeswitch] language detection skipped ({type(exc).__name__}: {exc})")
        return segments, dominant

    if not minority:
        return segments, dominant

    # Pass 2: the OTHER language over the WHOLE file (full context = far fewer
    # mis-hears than re-transcribing each short clip alone), then graft its text
    # onto the minority segments by time overlap (greedy; no clip reused twice).
    other_segs, _ = transcribe(audio_path, language=other, task="transcribe", allow_batched=False)
    used: set[int] = set()
    fixed = 0
    for i in minority:
        a, b = segments[i]["start_seconds"], segments[i]["end_seconds"]
        parts: list[str] = []
        for j, o in enumerate(other_segs):
            if j in used:
                continue
            odur = o["end_seconds"] - o["start_seconds"]
            overlap = min(o["end_seconds"], b) - max(o["start_seconds"], a)
            # graft an other-language segment only if it MOSTLY sits inside this
            # segment - stops one long spanning run from being pulled in whole.
            if odur > 0 and overlap > 0.5 * odur:
                parts.append(o["text"].strip())
                used.add(j)
        text = " ".join(p for p in parts if p).strip()
        if text:
            segments[i]["text"] = text
            segments[i]["language"] = other
            fixed += 1
    if fixed:
        print(f"[codeswitch] filled {fixed} minority-language segment(s) from a full {other} pass")
    return segments, dominant
