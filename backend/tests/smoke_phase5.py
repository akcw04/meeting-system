"""Phase 5 smoke test - speaker diarization with pyannote.audio.

Builds on Phase 4 (word-level alignment). After alignment, the pipeline
runs pyannote.audio diarization and saves speakers + segment->speaker
assignments to the DB.

On first run, pyannote downloads its diarization-3.1 + segmentation-3.0
checkpoints (~50 MB combined) into the Hugging Face cache. Requires
HF_TOKEN to be set in backend/.env.

The VOiCES test clip is single-speaker, so we expect exactly 1 speaker
in the output. Multi-speaker verification needs a real recording with
2+ distinct voices.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

FIXTURE = BACKEND / "tests" / "fixtures" / "voices_test.wav"
FIXTURE_URL = (
    "https://download.pytorch.org/torchaudio/tutorial-assets/"
    "Lab41-SRI-VOiCES-src-sp0307-ch127535-sg0042.wav"
)


def ensure_fixture() -> None:
    if FIXTURE.exists():
        return
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(FIXTURE_URL, FIXTURE)


def preload_models() -> None:
    print("Preloading models (first run downloads them)...")
    from app.pipeline.transcribe import get_whisper_model
    from app.pipeline.align import get_align_model
    from app.pipeline.diarize import get_diarization_pipeline

    t0 = time.time()
    get_whisper_model()
    print(f"  Whisper ready in {time.time() - t0:.1f}s.")

    t0 = time.time()
    get_align_model("en")
    print(f"  Alignment model ready in {time.time() - t0:.1f}s.")

    t0 = time.time()
    get_diarization_pipeline()
    print(f"  Diarization pipeline ready in {time.time() - t0:.1f}s.")


def main() -> int:
    ensure_fixture()
    preload_models()
    print("\n=== Phase 5 smoke test ===")

    with TestClient(app) as client:
        with FIXTURE.open("rb") as f:
            r = client.post(
                "/meetings",
                files={"file": (FIXTURE.name, f, "audio/wav")},
                data={
                    "title": "Phase 5 diarization test",
                    "primary_language": "en",
                },
            )
        assert r.status_code == 201, r.text
        meeting_id = r.json()["id"]
        print(f"POST /meetings -> 201, meeting_id={meeting_id}")
        assert r.json()["expected_speakers"] is None, "auto-detect expected"

        prev_status = None
        for attempt in range(240):
            r = client.get(f"/meetings/{meeting_id}")
            s = r.json()["status"]
            if s != prev_status:
                print(f"  attempt {attempt+1}: status={s}")
            prev_status = s
            if s in ("diarized", "ready") or s.startswith("categorize_failed"):  # at-or-past-diarized (pipeline now runs to ready; fixed 2026-07-17)
                break
            if s.startswith("error"):
                raise RuntimeError(f"Pipeline failed: {s}")
            time.sleep(1)
        else:
            raise TimeoutError("Pipeline did not reach the diarized-or-later state in 240 seconds")

        r = client.get(f"/meetings/{meeting_id}/transcript")
        assert r.status_code == 200, r.text
        t = r.json()
        print("\nGET /transcript:")
        print(f"  detected_language: {t['detected_language']}")
        print(f"  segment_count:     {t['segment_count']}")
        print(f"  speakers:          {[(s['label'], s['id']) for s in t['speakers']]}")
        for seg in t["segments"]:
            print(
                f"  segment speaker_id={seg['speaker_id']} "
                f"[{seg['start_seconds']:5.2f}-{seg['end_seconds']:5.2f}s]: "
                f"{seg['text']!r}"
            )

        # VOiCES is single-speaker, so we expect exactly 1
        assert len(t["speakers"]) == 1, (
            f"Expected 1 speaker for single-speaker clip, got {len(t['speakers'])}"
        )
        assert t["speakers"][0]["label"] == "Speaker 1"
        # Every segment with speech should be attributed to that speaker
        attributed = [s for s in t["segments"] if s["speaker_id"] is not None]
        assert len(attributed) >= 1, "At least one segment should have a speaker"
        speaker_id = t["speakers"][0]["id"]
        for seg in attributed:
            assert seg["speaker_id"] == speaker_id, (
                f"Segment {seg['id']} expected speaker_id={speaker_id}, got {seg['speaker_id']}"
            )

    # leave no residue: remove the test meeting (DB rows + files)
    import subprocess
    subprocess.run(
        [sys.executable, str(BACKEND / "tests" / "delete_meeting.py"), str(meeting_id)],
        check=False,
    )

    print("\nPhase 5 smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
