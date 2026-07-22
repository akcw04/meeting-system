"""Phase 2 smoke test - upload an audio file, verify VAD pipeline runs.

Uses the JFK inaugural address sample (11 sec, public domain) shipped by
the Whisper repo. Downloaded on first run, cached locally afterwards.
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
    print(f"Downloading {FIXTURE.name} from {FIXTURE_URL} ...")
    urllib.request.urlretrieve(FIXTURE_URL, FIXTURE)
    print(f"  -> {FIXTURE} ({FIXTURE.stat().st_size} bytes)")


def main() -> int:
    ensure_fixture()
    print("=== Phase 2 smoke test ===")

    with TestClient(app) as client:
        # 1. Upload the file
        with FIXTURE.open("rb") as f:
            response = client.post(
                "/meetings",
                files={"file": (FIXTURE.name, f, "audio/wav")},
                data={"title": "VOiCES speech test"},
            )
        print(f"POST /meetings -> {response.status_code}")
        assert response.status_code == 201, response.text
        meeting = response.json()
        meeting_id = meeting["id"]
        print(f"  meeting_id={meeting_id}, status={meeting['status']}")

        # 2. Poll until the pipeline finishes. Since the pipeline grew, the
        # runner carries a meeting past 'diarized' straight to 'ready' (or
        # 'categorize_failed:' when Ollama is down - the transcript, which is
        # what this phase tests, exists either way). Under TestClient the
        # background task runs synchronously inside POST, so intermediate
        # states may never be observable - accept any at-or-past-diarized
        # state. (Stale 'diarized'-only wait fixed 2026-07-17; the old
        # vad_segments.json sidecar check was removed with the standalone VAD
        # stage on 2026-06-14 - Silero VAD now runs inside transcription.)
        prev_status = None
        for attempt in range(240):
            r = client.get(f"/meetings/{meeting_id}")
            status_text = r.json()["status"]
            if status_text != prev_status:
                print(f"  attempt {attempt+1}: status={status_text}")
            prev_status = status_text
            if status_text in ("diarized", "ready") or status_text.startswith(
                "categorize_failed"
            ):
                break
            if status_text.startswith("error"):
                raise RuntimeError(f"Pipeline failed: {status_text}")
            time.sleep(1)
        else:
            raise TimeoutError("Pipeline did not finish in 240 seconds")

        # 3. Inspect the generated artifacts. VAD evidence is now the
        # transcript itself: Silero VAD (inside Faster-Whisper) must have
        # found speech for any segment to exist.
        meeting_dir = Path("./data/uploads") / str(meeting_id)
        audio_wav = meeting_dir / "audio.wav"
        assert audio_wav.exists(), f"audio.wav missing at {audio_wav}"

        t = client.get(f"/meetings/{meeting_id}/transcript").json()
        print(f"\nVAD-filtered transcription found {t['segment_count']} speech segment(s):")
        for s in t["segments"]:
            print(f"  {s['start_seconds']:6.2f} - {s['end_seconds']:6.2f} s")
        assert t["segment_count"] >= 1, "Should detect at least one speech segment"

        # 4. Confirm the meeting record's final state
        final = client.get(f"/meetings/{meeting_id}").json()
        print(f"\nFinal meeting state:")
        print(f"  status:           {final['status']}")
        print(f"  duration_seconds: {final['duration_seconds']}")
        print(f"  audio_path:       {final['audio_path']}")

        # pipeline runs through to the end ('ready'; 'diarized'/'categorize_failed'
        # tolerated so this phase doesn't depend on Ollama being up)
        assert final["status"] in ("ready", "diarized") or final["status"].startswith(
            "categorize_failed"
        ), final["status"]
        assert 1 < (final["duration_seconds"] or 0) < 120, (
            f"Duration {final['duration_seconds']} out of expected range"
        )
        assert final["audio_path"].endswith("audio.wav")

    # leave no residue: remove the test meeting (DB rows + files)
    import subprocess
    subprocess.run(
        [sys.executable, str(BACKEND / "tests" / "delete_meeting.py"), str(meeting_id)],
        check=False,
    )

    print("\nPhase 2 smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
