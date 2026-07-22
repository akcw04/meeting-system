"""Audio preprocessing - pure functions, no DB writes.

`extract_audio_to_wav` runs FFmpeg to normalize any input format
(MP4/MP3/WAV/M4A/WebM) into mono 16 kHz 16-bit PCM WAV.

`run_vad` runs Silero VAD on the WAV and returns speech timestamps.

The orchestrator that calls these and updates the DB lives in
app/pipeline/runner.py (Phase 3+).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import soundfile as sf
from silero_vad import get_speech_timestamps, load_silero_vad, read_audio

from app.config import settings


_silero_model = None


def _get_silero_model():
    global _silero_model
    if _silero_model is None:
        _silero_model = load_silero_vad()
    return _silero_model


def _resolve_ffmpeg() -> str:
    """Find ffmpeg.exe. Honors FFMPEG_PATH in .env, then falls back to PATH lookup."""
    if settings.ffmpeg_path:
        if Path(settings.ffmpeg_path).is_file():
            return settings.ffmpeg_path
        raise RuntimeError(
            f"FFMPEG_PATH is set to '{settings.ffmpeg_path}' in .env but no file exists there."
        )
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "ffmpeg not found. Install from https://www.gyan.dev/ffmpeg/builds/ and add to PATH, "
        "or set FFMPEG_PATH=<absolute path to ffmpeg.exe> in backend/.env."
    )


def extract_audio_to_wav(input_path: Path, output_wav: Path) -> float:
    """Use FFmpeg to convert any audio/video into mono 16 kHz 16-bit PCM WAV.

    Returns the duration in seconds.
    """
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _resolve_ffmpeg(),
        "-y",
        "-i", str(input_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(output_wav),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed for {input_path.name}:\n{result.stderr[-1000:]}"
        )
    info = sf.info(str(output_wav))
    return float(info.duration)


def run_vad(wav_path: Path) -> list[tuple[float, float]]:
    """Run Silero VAD. Returns [(start_seconds, end_seconds), ...] of speech.

    Long audio is processed in chunks. Silero VAD's TorchScript model has
    internal buffer limits and crashes on audio longer than ~15 minutes
    when fed in one shot. We chunk into 10-minute windows and stitch the
    results back together, adjusting timestamps for each chunk's offset.
    """
    model = _get_silero_model()
    sample_rate = 16000
    wav = read_audio(str(wav_path), sampling_rate=sample_rate)
    total_samples = wav.shape[0]

    chunk_samples = 10 * 60 * sample_rate  # 10-minute chunks

    if total_samples <= chunk_samples:
        # Short enough for a single pass.
        timestamps = get_speech_timestamps(
            wav, model, sampling_rate=sample_rate, return_seconds=True
        )
        return [(float(t["start"]), float(t["end"])) for t in timestamps]

    # Long audio: chunked processing.
    out: list[tuple[float, float]] = []
    for chunk_start in range(0, total_samples, chunk_samples):
        chunk_end = min(chunk_start + chunk_samples, total_samples)
        chunk = wav[chunk_start:chunk_end]
        offset_sec = chunk_start / sample_rate
        try:
            chunk_ts = get_speech_timestamps(
                chunk, model, sampling_rate=sample_rate, return_seconds=True
            )
        except Exception as exc:
            # Don't lose the entire VAD pass if one chunk fails - log and skip.
            print(
                f"[VAD] chunk {offset_sec:.0f}-{chunk_end / sample_rate:.0f}s failed: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        for t in chunk_ts:
            out.append(
                (float(t["start"]) + offset_sec, float(t["end"]) + offset_sec)
            )
    return out
