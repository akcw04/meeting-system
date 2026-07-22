"""Phase 4 smoke test - WhisperX word-level alignment.

Builds on Phase 3 (Whisper transcription). After transcribing,
the pipeline runs WhisperX alignment and saves per-word timestamps.

On first run, WhisperX downloads its alignment model (a wav2vec 2.0
checkpoint, ~360 MB for English) into ~/.cache/torch/.
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
    from app.pipeline.transcribe import get_whisper_model
    from app.pipeline.align import get_align_model

    print("Preloading Whisper model...")
    t0 = time.time()
    get_whisper_model()
    print(f"  Whisper ready in {time.time() - t0:.1f}s.")

    print("Preloading WhisperX alignment model (English)...")
    t0 = time.time()
    get_align_model("en")
    print(f"  Alignment model ready in {time.time() - t0:.1f}s.")


def main() -> int:
    ensure_fixture()
    preload_models()
    print("\n=== Phase 4 smoke test ===")

    with TestClient(app) as client:
        with FIXTURE.open("rb") as f:
            r = client.post(
                "/meetings",
                files={"file": (FIXTURE.name, f, "audio/wav")},
                data={"title": "Phase 4 alignment test", "primary_language": "en"},
            )
        assert r.status_code == 201, r.text
        meeting_id = r.json()["id"]
        print(f"POST /meetings -> 201, meeting_id={meeting_id}")

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
        assert r.status_code == 200
        t = r.json()
        print(f"\nGET /transcript:")
        print(f"  detected_language: {t['detected_language']}")
        print(f"  segment_count:     {t['segment_count']}")
        for seg in t["segments"]:
            print(f"  segment [{seg['start_seconds']:5.2f}-{seg['end_seconds']:5.2f}s]: {seg['text']!r}")
            for w in seg["words"][:10]:
                print(f"    [{w['start_seconds']:5.2f}-{w['end_seconds']:5.2f}s] {w['text']!r} (score={w['score']})")
            if len(seg["words"]) > 10:
                print(f"    ... and {len(seg['words']) - 10} more")

        total_words = sum(len(seg["words"]) for seg in t["segments"])
        assert total_words >= 5, f"Expected >=5 words from the VOiCES clip, got {total_words}"

        # Word durations should be reasonable (most words < 1 sec)
        all_words = [w for seg in t["segments"] for w in seg["words"]]
        avg_dur = sum(w["end_seconds"] - w["start_seconds"] for w in all_words) / len(all_words)
        print(f"\n  total words:     {total_words}")
        print(f"  avg word duration: {avg_dur:.3f}s")
        assert 0.05 < avg_dur < 1.5, f"Avg word duration {avg_dur:.3f}s out of plausible range"

    # leave no residue: remove the test meeting (DB rows + files)
    import subprocess
    subprocess.run(
        [sys.executable, str(BACKEND / "tests" / "delete_meeting.py"), str(meeting_id)],
        check=False,
    )

    print("\nPhase 4 smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
