"""GPU memory management for the 6 GB VRAM ceiling (RTX 4050 Laptop).

The pipeline loads multiple deep-learning models that each consume
significant VRAM:

    Faster-Whisper (small)        ~1 GB
    Faster-Whisper (large-v3 Int8) ~4-5 GB
    WhisperX alignment (wav2vec2) ~1 GB
    pyannote.audio diarization    ~1 GB
    Llama 3.1 8B (via Ollama)     ~5 GB   (separate process)

On a 6 GB card they can't all coexist. This module provides:

    report_vram(label)         -> one-line VRAM usage string
    vram_used_mb()             -> int, current usage
    empty_cache()              -> torch.cuda.empty_cache + gc
    free_whisper()             -> drop the cached Whisper model
    free_alignment()           -> drop the cached WhisperX model
    free_diarization()         -> drop the cached pyannote pipeline
    free_silero_vad()          -> drop the cached VAD model
    free_all_torch_models()    -> all of the above (call before invoking
                                  Ollama in Phase 7)

We deliberately keep models loaded between meetings (model loads cost
~5-10 seconds each). Free only when GPU pressure demands it — before
launching the Llama LLM, before a giant Whisper-Large-v3 load on a tight
card, or in a CLI tool.
"""
from __future__ import annotations

import gc

import torch


def vram_used_mb() -> int:
    """Total VRAM currently in use on the device, in MB.

    Uses the driver-level CUDA query rather than torch.cuda.memory_allocated()
    because some pipeline components (Faster-Whisper via CTranslate2,
    Silero VAD via ONNX Runtime) allocate VRAM outside PyTorch's tracker.
    """
    if not torch.cuda.is_available():
        return 0
    free, total = torch.cuda.mem_get_info()
    return (total - free) // (1024 * 1024)


def vram_total_mb() -> int:
    if not torch.cuda.is_available():
        return 0
    return torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)


def report_vram(label: str = "") -> str:
    """One-line VRAM usage report. Use freely as a debug breadcrumb."""
    if not torch.cuda.is_available():
        return f"[GPU] no CUDA (CPU mode) ({label})"
    used = vram_used_mb()
    total = vram_total_mb()
    pct = (used / total) * 100 if total > 0 else 0
    return f"[GPU] {label:<28} {used:>5} / {total} MB ({pct:5.1f}%)"


def empty_cache() -> None:
    """Force torch + cpython to release everything they can.

    Order matters: gc first to drop Python references, then torch's
    cache, then synchronize so we observe the freed state immediately.
    """
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()


def free_whisper() -> None:
    """Drop the cached Whisper model and release its VRAM."""
    from app.pipeline import transcribe
    if transcribe._whisper_model is not None:
        del transcribe._whisper_model
        transcribe._whisper_model = None
        transcribe._loaded_name = None
    transcribe._batched_pipeline = None
    empty_cache()


def free_alignment() -> None:
    """Drop the cached WhisperX alignment model and release its VRAM."""
    from app.pipeline import align
    if align._align_model is not None:
        del align._align_model
        align._align_model = None
    if align._align_metadata is not None:
        align._align_metadata = None
    align._loaded_language = None
    empty_cache()


def free_diarization() -> None:
    """Drop the cached pyannote pipeline and release its VRAM."""
    from app.pipeline import diarize
    if diarize._pipeline is not None:
        del diarize._pipeline
        diarize._pipeline = None
    empty_cache()


def free_silero_vad() -> None:
    """Drop the cached Silero VAD model and release its VRAM."""
    from app.pipeline import audio
    if audio._silero_model is not None:
        del audio._silero_model
        audio._silero_model = None
    empty_cache()


def free_all_torch_models() -> None:
    """Release every pipeline model held in this process.

    Call before invoking Ollama (Phase 7) so Llama 3.1 has the full
    GPU to itself. Models will re-load on next use (cost: ~5-15 sec
    depending on which one).
    """
    free_whisper()
    free_alignment()
    free_diarization()
    free_silero_vad()
