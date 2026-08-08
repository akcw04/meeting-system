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
    """For each transcript segment, pick the diarization turn that overlaps
    most with it in time. Attach the speaker label to the segment.

    If overlap is below `min_overlap_ratio` of the segment duration (background
    noise, music, silence), the segment's speaker stays None and the UI shows
    it as 'unknown'.
    """
    enriched: list[dict] = []
    for seg in segments:
        s_start = seg["start_seconds"]
        s_end = seg["end_seconds"]
        s_dur = max(0.001, s_end - s_start)

        best_speaker: str | None = None
        best_overlap = 0.0
        for turn in diarization:
            overlap_start = max(s_start, turn["start_seconds"])
            overlap_end = min(s_end, turn["end_seconds"])
            overlap = max(0.0, overlap_end - overlap_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = turn["speaker"]

        if best_speaker is not None and (best_overlap / s_dur) < min_overlap_ratio:
            best_speaker = None

        enriched.append({**seg, "speaker": best_speaker})
    return enriched
