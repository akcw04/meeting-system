"""Phase 3 smoke test - end-to-end pipeline including Whisper.

Uses the same VOiCES sample as Phase 2. On first run, Faster-Whisper
downloads the configured model (~480 MB for 'small') into data/models/.
Subsequent runs are fast.
"""
from __future__ import annotations

import json
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
    print(f"Downloading {FIXTURE.name} ...")
    urllib.request.urlretrieve(FIXTURE_URL, FIXTURE)


def preload_whisper() -> None:
    """Eager-load Whisper so the test isn't dominated by first-time download."""
    from app.pipeline.transcribe import get_whisper_model
    print("Preloading Whisper model (downloads on first run, then cached)...")
    t0 = time.time()
    get_whisper_model()
    print(f"  Whisper ready in {time.time() - t0:.1f}s.")


def main() -> int:
    ensure_fixture()
    preload_whisper()
    print("\n=== Phase 3 smoke test ===")

    with TestClient(app) as client:
        with FIXTURE.open("rb") as f:
            r = client.post(
                "/meetings",
                files={"file": (FIXTURE.name, f, "audio/wav")},
                data={"title": "Phase 3 transcription test", "primary_language": "en"},
            )
        print(f"POST /meetings -> {r.status_code}")
        assert r.status_code == 201, r.text
        meeting = r.json()
        meeting_id = meeting["id"]
        print(f"  meeting_id={meeting_id}, primary_language={meeting['primary_language']}")
        assert meeting["primary_language"] == "en"

        # Poll until the pipeline finishes. The runner now continues past
        # transcription into alignment + diarization, ending at 'diarized'.
        # Transcript data is available once we reach that terminal state.
        prev_status = None
        for attempt in range(240):
            r = client.get(f"/meetings/{meeting_id}")
            status_text = r.json()["status"]
            if status_text != prev_status:
                print(f"  attempt {attempt+1}: status={status_text}")
            prev_status = status_text
            if status_text in ("diarized", "ready") or status_text.startswith("categorize_failed"):  # at-or-past-diarized (pipeline now runs to ready; fixed 2026-07-17)
                break
            if status_text.startswith("error"):
                raise RuntimeError(f"Pipeline failed: {status_text}")
            time.sleep(1)
        else:
            raise TimeoutError("Pipeline did not reach the diarized-or-later state in 240 seconds")

        # Fetch the transcript
        r = client.get(f"/meetings/{meeting_id}/transcript")
        assert r.status_code == 200, r.text
        transcript = r.json()
        print(f"\nGET /meetings/{meeting_id}/transcript -> {r.status_code}")
        print(f"  detected_language: {transcript['detected_language']}")
        print(f"  segment_count:     {transcript['segment_count']}")
        for s in transcript["segments"]:
            print(f"  [{s['start_seconds']:5.2f}-{s['end_seconds']:5.2f}s] {s['text']!r}")

        assert transcript["segment_count"] >= 1, "Should have at least one segment"
        assert transcript["detected_language"] == "en", (
            f"Expected English detection on VOiCES sample, got {transcript['detected_language']}"
        )
        # The VOiCES clip is a short English sentence; expect some real text.
        all_text = " ".join(s["text"] for s in transcript["segments"])
        assert len(all_text.strip()) > 10, f"Transcript text too short: {all_text!r}"

    # leave no residue: remove the test meeting (DB rows + files)
    import subprocess
    subprocess.run(
        [sys.executable, str(BACKEND / "tests" / "delete_meeting.py"), str(meeting_id)],
        check=False,
    )

    print("\nPhase 3 smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
