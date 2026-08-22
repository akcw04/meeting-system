"""Speaker diarization with pyannote.audio.

Diarization answers "who spoke when" - it splits the audio into time ranges
each tagged with a speaker label (SPEAKER_00, SPEAKER_01, ...). It does NOT
know real names; that mapping happens later in the HITL dashboard (Phase 9).

How it works under the hood:
  1. A fine-grained segmenter (pyannote/segmentation-3.0) splits the audio
     into short voice-active windows.
  2. A speaker embedding model produces a fixed-length "voice fingerprint"
     for each window.
  3. Agglomerative clustering groups windows with similar fingerprints into
     speaker clusters. The number of speakers is inferred from the clustering;
     you can override with the `num_speakers` hint when known.

Gotcha: pyannote/speaker-diarization-3.1 is a gated model. The user must:
  - Have a Hugging Face token (set as HF_TOKEN in backend/.env)
  - Have accepted the license at https://hf.co/pyannote/speaker-diarization-3.1
"""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

# pyannote 4.x warns at import time that torchcodec (its optional built-in
# file decoder) cannot load its DLLs. That decoder is never used here: this
# module always feeds pyannote preloaded in-memory audio as a
# {"waveform", "sample_rate"} dictionary (see docs/BUGS.md #9), which is the
# exact workaround the warning itself recommends. Silenced for clean startup.
warnings.filterwarnings(
    "ignore", category=UserWarning, module=r"pyannote\.audio\.core\.io"
)
from pyannote.audio import Pipeline

from app.config import settings


_pipeline: Pipeline | None = None


def get_diarization_pipeline() -> Pipeline:
    """Load (and cache) the speaker-diarization-3.1 pipeline."""
    global _pipeline
    if _pipeline is None:
        if not settings.hf_token:
            raise RuntimeError(
                "HF_TOKEN not set in backend/.env. pyannote.audio needs it to "
                "download the speaker-diarization-3.1 model. Create a free "
                "'read' token at https://huggingface.co/settings/tokens, then "
                "add HF_TOKEN=hf_... to backend/.env. The three pyannote model "
                "licences must also be accepted once on that account."
            )
        # pyannote.audio 4.x renamed `use_auth_token` -> `token`. We try the
        # new name first and fall back for older installs.
        try:
            _pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=settings.hf_token,
            )
        except TypeError:
            _pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=settings.hf_token,
            )
        if torch.cuda.is_available():
            _pipeline.to(torch.device("cuda"))
    return _pipeline


def diarize(
    audio_path: Path, expected_speakers: int | None = None
) -> tuple[list[dict], dict | None]:
    """Run speaker diarization.

    Returns ``(turns, embeddings)``:
      - ``turns`` - ordered list of ``{start_seconds, end_seconds, speaker}``
        where ``speaker`` is a string label like ``'SPEAKER_00'``.
      - ``embeddings`` - ``{label: 256-d np.ndarray voiceprint}``, or ``None``
        if pyannote returned none we can trust. Consumed by
        ``merge_phantom_speakers`` to fold over-detected speakers together.

    If `expected_speakers` is given, pyannote uses it as a hard hint;
    otherwise it auto-detects (typically 1-10 speakers, depending on audio).
    """
    pipeline = get_diarization_pipeline()

    # Load the audio in-memory and hand pyannote a {waveform, sample_rate}
    # dict instead of a file path. This bypasses pyannote 4.x's torchcodec
    # decoder (which needs FFmpeg *shared libraries* that aren't in the
    # standard "essentials" FFmpeg build). Our audio.wav is always plain
    # mono 16 kHz PCM from the FFmpeg step in audio.py, so soundfile reads
    # it natively with no extra dependency.
    waveform_np, sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
    # soundfile gives (time, channels); pyannote wants (channels, time)
    waveform = torch.from_numpy(waveform_np.T).contiguous()
    file = {"waveform": waveform, "sample_rate": int(sample_rate)}

    kwargs: dict = {}
    if expected_speakers is not None and expected_speakers > 0:
        kwargs["num_speakers"] = int(expected_speakers)

    output = pipeline(file, **kwargs)

    # pyannote.audio 4.x returns a DiarizeOutput wrapper; the actual
    # speaker timeline is the .speaker_diarization Annotation. Older
    # versions returned the Annotation directly.
    annotation = getattr(output, "speaker_diarization", output)

    turns: list[dict] = []
    for turn, _, speaker_label in annotation.itertracks(yield_label=True):
        turns.append(
            {
                "start_seconds": float(turn.start),
                "end_seconds": float(turn.end),
                "speaker": speaker_label,
            }
        )

    # pyannote 4.x also exposes per-speaker voiceprints on the wrapper, as an
    # ndarray of shape (num_speakers, 256). Adapt it to {label: vector} for the
    # phantom-merge step; None when unavailable (older pyannote / odd shape).
    embeddings = _build_embedding_map(annotation, getattr(output, "speaker_embeddings", None))
    return turns, embeddings


def _build_embedding_map(annotation, emb_array) -> dict | None:
    """Adapt pyannote 4.x's ``speaker_embeddings`` ndarray to {label: vector}.

    The array has shape ``(num_speakers, dim)`` with row ``i`` aligned to the
    i-th label in sorted order (labels are zero-padded ``'SPEAKER_NN'``, so
    alphabetical == numeric). Returns ``None`` when the shape can't be trusted,
    so the caller safely skips the merge instead of risking a wrong mapping.
    """
    if emb_array is None:
        return None
    arr = np.asarray(emb_array)
    if arr.ndim != 2:
        return None
    labels = sorted(annotation.labels())
    if arr.shape[0] != len(labels):
        return None
    mapping: dict[str, np.ndarray] = {}
    for i, label in enumerate(labels):
        vec = arr[i].astype("float32", copy=False)
        if np.isnan(vec).any():
            continue  # speaker without a usable voiceprint - leave it untouched
        mapping[label] = vec
    return mapping or None


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return -1.0
    return float(np.dot(a, b) / (na * nb))


def merge_phantom_speakers(
    turns: list[dict],
    embeddings: dict | None,
    max_seconds: float,
) -> list[dict]:
    """Fold "phantom" speakers into their acoustically nearest real speaker.

    pyannote sometimes over-segments, emitting extra speakers that hold only a
    second or two of audio (clustering noise). For each speaker whose TOTAL
    talk-time is below ``max_seconds``, we find the non-phantom speaker with the
    most similar voiceprint (highest cosine similarity) and remap its turns onto
    that speaker.

    Returns a NEW turns list (input is not mutated). No-op - returns ``turns``
    unchanged - when there are fewer than two speakers, embeddings are
    unavailable, or nothing qualifies as a phantom, so it can never collapse a
    genuine multi-speaker meeting into one. Callers should skip it entirely when
    the user gave an explicit speaker count (pyannote already clustered to
    exactly that many).
    """
    if not turns or not embeddings or len(embeddings) < 2:
        return turns

    talk_time: dict[str, float] = {}
    for t in turns:
        talk_time[t["speaker"]] = talk_time.get(t["speaker"], 0.0) + (
            t["end_seconds"] - t["start_seconds"]
        )

    phantoms = [
        lbl for lbl, secs in talk_time.items()
        if secs < max_seconds and lbl in embeddings
    ]
    targets = [lbl for lbl in embeddings if lbl not in phantoms]
    if not phantoms or not targets:
        return turns

    remap: dict[str, str] = {}
    for ph in phantoms:
        vec = embeddings[ph]
        best_label, best_sim = None, -2.0
        for tg in targets:
            sim = _cosine(vec, embeddings[tg])
            if sim > best_sim:
                best_sim, best_label = sim, tg
        if best_label is not None:
            remap[ph] = best_label
    if not remap:
        return turns

    return [{**t, "speaker": remap.get(t["speaker"], t["speaker"])} for t in turns]


def assign_speakers_to_segments(
    segments: list[dict],
    diarization: list[dict],
    min_overlap_ratio: float = 0.3,
) -> list[dict]:
    """For each transcript segment, attach the speaker who talks most in it.

    Overlap is accumulated PER SPEAKER across all of that speaker's turns, and
    the segment goes to whoever holds the most of it. `min_overlap_ratio` then
    judges whether the segment is mostly speech at all - if the diarizer found
    little speech across the whole span (background noise, music, silence), the
    speaker stays None and the UI shows it as 'unknown'.

    Deliberately NOT "the single turn with the largest overlap" (the original
    rule). That compared ONE turn against the whole segment length, so a long
    segment demanded one improbably long unbroken turn to clear the threshold:
    a 28-second segment needed an 8.4-second turn, while real conversational
    turns here averaged under two seconds. Every long segment therefore failed
    the test and silently lost its speaker. On a real test recording that left
    7 of 9 segments labelled 'unknown' even though the diarizer had covered
    them with 108 turns of speech. Whisper produces long segments whenever
    people talk without pausing, so this was not an edge case.
    """
    enriched: list[dict] = []
    for seg in segments:
        s_start = seg["start_seconds"]
        s_end = seg["end_seconds"]
        s_dur = max(0.001, s_end - s_start)

        per_speaker: dict[str, float] = {}
        for turn in diarization:
            overlap_start = max(s_start, turn["start_seconds"])
            overlap_end = min(s_end, turn["end_seconds"])
            overlap = overlap_end - overlap_start
            if overlap > 0:
                per_speaker[turn["speaker"]] = per_speaker.get(turn["speaker"], 0.0) + overlap

        speaker: str | None = None
        if per_speaker:
            speech = sum(per_speaker.values())
            # Is this segment mostly speech? (the original intent of the guard)
            if speech / s_dur >= min_overlap_ratio:
                speaker = max(per_speaker.items(), key=lambda kv: kv[1])[0]

        enriched.append({**seg, "speaker": speaker})
    return enriched


def _word_speaker(word: dict, diarization: list[dict]) -> str | None:
    """The speaker with the most diarization overlap over a single word's span.

    None when no turn overlaps the word at all (a word sitting in silence, e.g.
    a trailing breath the aligner kept) - the caller fills those from context.
    """
    ws, we = word["start_seconds"], word["end_seconds"]
    per: dict[str, float] = {}
    for turn in diarization:
        overlap = min(we, turn["end_seconds"]) - max(ws, turn["start_seconds"])
        if overlap > 0:
            per[turn["speaker"]] = per.get(turn["speaker"], 0.0) + overlap
    if not per:
        return None
    return max(per.items(), key=lambda kv: kv[1])[0]


# A run of at most this many words AND shorter than this many seconds is treated
# as diarization noise at a turn boundary - a stray word or two the diarizer
# flipped to another speaker mid-utterance - and is absorbed into an adjacent run
# rather than left as a sub-second fragment. Genuine conversational turns run
# longer than this.
_SMOOTH_MAX_WORDS = 2
_SMOOTH_MAX_SECONDS = 0.8


def _smooth_speaker_runs(runs: list[dict]) -> list[dict]:
    """Absorb tiny speaker runs into a neighbour, then coalesce same-speaker runs.

    Word-level diarization occasionally flips a stray word to another speaker
    where two turns meet (or where pyannote briefly over-detects a third
    speaker), shattering one clean turn into sub-second fragments. Each tiny run
    is merged into whichever neighbour holds more words - the surrounding speech
    it most likely belongs to - and consecutive same-speaker runs are then joined.
    Iterates until stable so a cluster of fragments collapses cleanly; a lone run
    is returned unchanged.
    """
    def dur(run: dict) -> float:
        w = run["words"]
        return (w[-1]["end_seconds"] - w[0]["start_seconds"]) if w else 0.0

    for _ in range(len(runs)):  # bounded: each pass merges at least one, or stops
        if len(runs) <= 1:
            break
        merged_any = False
        for i, run in enumerate(runs):
            if len(run["words"]) <= _SMOOTH_MAX_WORDS and dur(run) < _SMOOTH_MAX_SECONDS:
                prev = runs[i - 1] if i > 0 else None
                nxt = runs[i + 1] if i < len(runs) - 1 else None
                if prev is None and nxt is None:
                    continue
                if prev is None:
                    run["speaker"] = nxt["speaker"]
                elif nxt is None:
                    run["speaker"] = prev["speaker"]
                else:
                    bigger = prev if len(prev["words"]) >= len(nxt["words"]) else nxt
                    run["speaker"] = bigger["speaker"]
                merged_any = True
        if not merged_any:
            break
        coalesced: list[dict] = []
        for run in runs:
            if coalesced and coalesced[-1]["speaker"] == run["speaker"]:
                coalesced[-1]["words"].extend(run["words"])
            else:
                coalesced.append(run)
        runs = coalesced
    return runs


def split_segments_by_speaker(
    segments: list[dict],
    diarization: list[dict],
    min_overlap_ratio: float = 0.3,
) -> list[dict]:
    """Attribute a speaker to every WORD and split each segment at speaker
    changes, so one coarse Whisper segment spanning several turns becomes one
    sub-segment per speaker instead of collapsing two voices under one label.

    Whisper groups continuous speech into blocks up to its 30-second window, so a
    single segment routinely covers a whole exchange (measured: a re-recorded
    two-person meeting produced ten segments averaging 26 s, each holding both
    speakers). `assign_speakers_to_segments` then stamps the WHOLE block with its
    majority speaker and the minority voice disappears. Splitting on the
    word-level diarization fixes that at the only granularity fine enough to carry
    a turn change - individual words, whose timings WhisperX already provides.

    Contract mirrors `assign_speakers_to_segments`: returns segments each with a
    `speaker` label (or None = unknown), ready to persist, and preserves each
    piece's `words`. A segment is returned UNCHANGED when it holds one speaker, so
    the original text/punctuation is kept wherever no split is needed; only a
    genuinely split segment has its text rebuilt from its words.

    Graceful degradation: a segment with no word timings (a language WhisperX
    cannot align, so `words` is empty) falls back to the whole-segment rule, and
    the same low-speech noise guard leaves near-silent segments unknown.
    """
    out: list[dict] = []
    for seg in segments:
        words = seg.get("words") or []
        if not words:
            out.extend(assign_speakers_to_segments([seg], diarization, min_overlap_ratio))
            continue

        # Noise guard: if the diarizer found little speech across the whole span,
        # leave it unknown and unsplit rather than carving up non-speech.
        s_start, s_end = seg["start_seconds"], seg["end_seconds"]
        s_dur = max(0.001, s_end - s_start)
        speech = 0.0
        for turn in diarization:
            overlap = min(s_end, turn["end_seconds"]) - max(s_start, turn["start_seconds"])
            if overlap > 0:
                speech += overlap
        if speech / s_dur < min_overlap_ratio:
            out.append({**seg, "speaker": None})
            continue

        labelled = [[w, _word_speaker(w, diarization)] for w in words]
        # Fill words that fell in silence from their neighbours: forward first
        # (inherit the speaker still talking), then back-fill any leading gap.
        last: str | None = None
        for pair in labelled:
            if pair[1] is None:
                pair[1] = last
            else:
                last = pair[1]
        nxt: str | None = None
        for pair in reversed(labelled):
            if pair[1] is None:
                pair[1] = nxt
            else:
                nxt = pair[1]

        distinct = {p[1] for p in labelled}
        if len(distinct) <= 1:
            # One speaker across the segment - keep it verbatim (no rebuild).
            out.append({**seg, "speaker": next(iter(distinct)) if distinct else None})
            continue

        # Multiple speakers - split into consecutive same-speaker runs.
        no_space = (seg.get("language") == "zh")
        runs: list[dict] = []
        for word, spk in labelled:
            if runs and runs[-1]["speaker"] == spk:
                runs[-1]["words"].append(word)
            else:
                runs.append({"speaker": spk, "words": [word]})

        # Absorb sub-second diarization-noise fragments at turn boundaries.
        runs = _smooth_speaker_runs(runs)
        if len({r["speaker"] for r in runs}) <= 1:
            # Smoothing collapsed the segment to one speaker - keep it verbatim.
            out.append({**seg, "speaker": runs[0]["speaker"] if runs else None})
            continue

        for run in runs:
            rw = run["words"]
            text = (
                "".join(w["text"] for w in rw)
                if no_space
                else " ".join(w["text"].strip() for w in rw)
            ).strip()
            out.append(
                {
                    "start_seconds": rw[0]["start_seconds"],
                    "end_seconds": rw[-1]["end_seconds"],
                    "text": text,
                    "language": seg.get("language"),
                    "speaker": run["speaker"],
                    "words": rw,
                }
            )
    return out
