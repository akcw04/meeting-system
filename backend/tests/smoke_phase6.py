"""Phase 6 smoke test - GPU memory management.

Loads each pipeline model in turn, prints VRAM usage, then frees it
and verifies the VRAM dropped. Confirms our free_*() helpers work.

Note: pyannote.audio actually loads onto CPU by default and only moves
to CUDA when called. So free_diarization() may not show a big VRAM
drop here - that's expected. The Phase 7 LLM step is where freeing
actually matters.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

import torch  # noqa: E402

from app.gpu import (  # noqa: E402
    free_alignment,
    free_all_torch_models,
    free_diarization,
    free_silero_vad,
    free_whisper,
    report_vram,
    vram_used_mb,
)


def main() -> int:
    if not torch.cuda.is_available():
        print("CUDA not available - this test only runs on GPU.")
        return 0

    print("=== Phase 6 smoke test: GPU memory management ===\n")
    baseline = vram_used_mb()
    print(report_vram("baseline"))

    # --- Whisper ---
    print()
    print("Loading Whisper...")
    from app.pipeline.transcribe import get_whisper_model
    get_whisper_model()
    after_whisper = vram_used_mb()
    print(report_vram("after Whisper load"))
    assert after_whisper > baseline + 100, (
        f"Whisper should add VRAM (was {baseline}, now {after_whisper})"
    )

    print("Freeing Whisper...")
    free_whisper()
    after_free_whisper = vram_used_mb()
    print(report_vram("after Whisper free"))
    assert after_free_whisper < after_whisper, (
        f"VRAM should drop after free (was {after_whisper}, now {after_free_whisper})"
    )

    # --- Alignment ---
    print()
    print("Loading WhisperX alignment model (English)...")
    from app.pipeline.align import get_align_model
    get_align_model("en")
    after_align = vram_used_mb()
    print(report_vram("after alignment load"))

    print("Freeing alignment...")
    free_alignment()
    print(report_vram("after alignment free"))

    # --- Diarization ---
    print()
    print("Loading pyannote diarization pipeline...")
    from app.pipeline.diarize import get_diarization_pipeline
    get_diarization_pipeline()
    print(report_vram("after diarization load"))

    print("Freeing diarization...")
    free_diarization()
    print(report_vram("after diarization free"))

    # --- Silero VAD ---
    print()
    print("Loading Silero VAD...")
    from app.pipeline.audio import _get_silero_model
    _get_silero_model()
    print(report_vram("after Silero VAD load"))

    print("Freeing Silero VAD...")
    free_silero_vad()
    print(report_vram("after Silero VAD free"))

    # --- Final cleanup ---
    print()
    print("Calling free_all_torch_models()...")
    free_all_torch_models()
    final = vram_used_mb()
    print(report_vram("after free_all"))

    # Final should be close to baseline. Allow a small slack since
    # CUDA contexts and tensors can leave small fragments.
    slack_mb = 100
    assert final <= baseline + slack_mb, (
        f"Final VRAM ({final} MB) should be near baseline ({baseline} MB) "
        f"within {slack_mb} MB slack."
    )

    print("\nPhase 6 smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
