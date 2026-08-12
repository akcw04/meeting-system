"""Faster-Whisper transcription - pure function, no DB writes.

First call downloads the model into `data/models/` (~480 MB for 'small',
~3 GB for 'large-v3'). Subsequent calls reuse the cached weights.

By default we auto-detect the language, but the caller can force one via the
`language` argument. The runner passes the user's `primary_language` when it
is a supported language (added after bug #13, where auto-detect misfired to
Maori on a long English meeting). 'auto' still lets Whisper detect per audio.

Languages: English, Mandarin and Bahasa Melayu, in any combination. A
Malaysian meeting routinely mixes all three in one sentence, so the
code-switch path (`transcribe_codeswitch`) is language-count-agnostic rather
than the fixed EN/ZH pair it started as.
"""
from __future__ import annotations

import re
from pathlib import Path

import soundfile as sf
from faster_whisper import BatchedInferencePipeline, WhisperModel

from app.config import settings


# Languages the system transcribes and can mix within one recording.
# 'ms' (Bahasa Melayu) joined 'en'/'zh' on 2026-08-07 - see DECISIONS.md.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "zh", "ms")

# Whisper treats Malay and Indonesian as near-identical (they share most of
# their phonology and orthography) and will happily label Malaysian speech
# 'id'. Since this system targets Malaysian meetings and does NOT offer
# Indonesian as a separate output language, an 'id' detection is folded into
# 'ms' rather than discarded - otherwise genuine Malay segments would be
# dropped from the code-switch routing entirely.
_LANGUAGE_ALIASES = {"id": "ms"}


def canonical_language(lang: str | None) -> str:
    """Fold a Whisper-reported code onto the language the system works in."""
    code = (lang or "").lower().strip()
    return _LANGUAGE_ALIASES.get(code, code)


_HAS_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def languages_in_segments(segments: list[dict]) -> list[str]:
    """Which languages the produced transcript ACTUALLY contains.

    Preferred over `detect_languages()` for recording what a meeting turned out
    to be. That function samples 25-second windows and asks Whisper for ONE
    language per window, which cannot surface a language that only appears in
    short bursts: a four-second Mandarin sentence inside a window dominated by
    English is simply outvoted. On a real three-language test recording that
    under-reported a genuinely trilingual meeting as bilingual, even though the
    code-switch pass had transcribed the Mandarin correctly.

    Reading the finished segments instead gives exactly the right granularity.
    Chinese characters in the text also count on their own: a per-segment
    language LABEL can be wrong, but the script it was written in cannot be.
    """
    found: set[str] = set()
    for seg in segments:
        lang = canonical_language(seg.get("language"))
        if lang:
            found.add(lang)
        if _HAS_CJK.search(seg.get("text") or ""):
            found.add("zh")
    return sorted(found)


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
    candidates: tuple[str, ...] = SUPPORTED_LANGUAGES,
) -> list[str]:
    """Sample windows across the audio and detect each window's spoken language.

    Used to spot code-switched recordings (English, Mandarin and Bahasa Melayu
    in any combination) BEFORE transcription, so the runner can route them.
    Reads only the sampled windows (not the whole file) so it stays cheap on
    multi-hour recordings.

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
                lang = canonical_language(
                    res[0] if isinstance(res, (tuple, list)) else res
                )
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
    # is 'en', not the source language Whisper detected. An auto-detected 'id'
    # is folded to 'ms' (see _LANGUAGE_ALIASES).
    output_lang = "en" if task == "translate" else canonical_language(info.language)

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
    candidates: tuple[str, ...] = SUPPORTED_LANGUAGES,
    progress_callback=None,
) -> tuple[list[dict], str]:
    """Accurate transcription of a code-switched (mixed-language) recording.

    Each part stays in the language it was spoken (NO translation), so the
    displayed transcript faithfully shows what was captured - which is what lets
    users verify accuracy and trust the system (DECISIONS 2026-06-21).

    Strategy - ONE FULL PASS PER LANGUAGE ACTUALLY PRESENT (not short
    per-segment clips, which lose context and mis-hear, e.g. "我" -> "宝宝"):
      1. the DOMINANT language over the whole file -> defines the segmentation
         we keep;
      2. for EACH minority language detected in the audio, another full pass in
         that language -> full-context text for that language's parts.
    Each segment is language-detected on its own audio and only flipped on a
    CONFIDENT result, so a dominant-language line is never turned into the wrong
    language. Minority segments take their text from their own language's pass,
    matched by time overlap.

    Generalised from the original fixed EN/ZH pair (2026-08-07) so a Malaysian
    meeting mixing English, Mandarin AND Bahasa Melayu is transcribed correctly
    in all three. Cost scales with the number of languages genuinely present -
    a pass is only run for a language some segment was confidently detected as,
    so a single-language stretch never pays for languages that never occur.

    Returns (segments, dominant_language).
    """
    model = get_whisper_model()
    others = [c for c in candidates if c != dominant]
    # Pass 1: whole file in the dominant language (sequential, not batched - the
    # batched pipeline merges turns into very coarse segments for some audio).
    # This pass defines the segmentation we keep.
    segments, _ = transcribe(
        audio_path, language=dominant, task="transcribe",
        allow_batched=False, progress_callback=progress_callback,
    )
    if not others or not segments:
        return segments, dominant

    # Which segments are a NON-dominant language, and which one? Detect on each
    # segment's audio; only flip on a confident result so dominant lines aren't
    # mislabelled. Grouped by language so each language is passed over once.
    minority_by_lang: dict[str, list[int]] = {}
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
                lang = canonical_language(
                    res[0] if isinstance(res, (tuple, list)) else res
                )
                prob = res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else 1.0
                if lang in others and (prob is None or prob >= 0.55):
                    minority_by_lang.setdefault(lang, []).append(i)
    except Exception as exc:  # best-effort; fall back to the dominant-only pass
        print(f"[codeswitch] language detection skipped ({type(exc).__name__}: {exc})")
        return segments, dominant

    if not minority_by_lang:
        return segments, dominant

    print(
        f"[codeswitch] dominant={dominant}; minority segments: "
        + ", ".join(f"{lang}={len(idx)}" for lang, idx in sorted(minority_by_lang.items()))
    )

    # One full pass per minority language over the WHOLE file (full context =
    # far fewer mis-hears than re-transcribing each short clip alone), then
    # graft its text onto that language's segments by time overlap. `used` is
    # per-pass: a clip from one language's pass is never reused twice, but the
    # passes are independent of each other.
    for lang in sorted(minority_by_lang, key=lambda k: -len(minority_by_lang[k])):
        indices = minority_by_lang[lang]
        other_segs, _ = transcribe(
            audio_path, language=lang, task="transcribe", allow_batched=False
        )
        used: set[int] = set()
        fixed = 0
        for i in indices:
            a, b = segments[i]["start_seconds"], segments[i]["end_seconds"]
            parts: list[str] = []
            for j, o in enumerate(other_segs):
                if j in used:
                    continue
                odur = o["end_seconds"] - o["start_seconds"]
                overlap = min(o["end_seconds"], b) - max(o["start_seconds"], a)
                # graft an other-language segment only if it MOSTLY sits inside
                # this segment - stops one long spanning run being pulled in whole.
                if odur > 0 and overlap > 0.5 * odur:
                    parts.append(o["text"].strip())
                    used.add(j)
            text = " ".join(p for p in parts if p).strip()
            if text:
                segments[i]["text"] = text
                segments[i]["language"] = lang
                fixed += 1
        if fixed:
            print(f"[codeswitch] filled {fixed} segment(s) from a full {lang} pass")
    return segments, dominant
