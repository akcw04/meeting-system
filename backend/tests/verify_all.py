"""One-shot verification of the whole pipeline (Phases 1-5).

Runs a single meeting through the entire backend and prints a readable
report of what each stage produced. Use this to eyeball the current
capabilities end-to-end.

Usage:
    cd backend
    .\\.venv\\Scripts\\activate
    python tests\\verify_all.py                 # uses the built-in English test clip
    python tests\\verify_all.py path\\to\\your\\meeting.mp3   # uses your own file
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

DEFAULT_FIXTURE = BACKEND / "tests" / "fixtures" / "voices_test.wav"
FIXTURE_URL = (
    "https://download.pytorch.org/torchaudio/tutorial-assets/"
    "Lab41-SRI-VOiCES-src-sp0307-ch127535-sg0042.wav"
)


def hr(title: str = "") -> None:
    print("\n" + "=" * 68)
    if title:
        print(title)
        print("=" * 68)


def main() -> int:
    audio_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURE
    if not audio_path.exists():
        if audio_path == DEFAULT_FIXTURE:
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"Downloading built-in test clip...")
            urllib.request.urlretrieve(FIXTURE_URL, audio_path)
        else:
            print(f"ERROR: file not found: {audio_path}")
            return 1

    hr("MEETING SYSTEM - END-TO-END VERIFICATION")
    print(f"Input file: {audio_path}")
    print("Stages exercised: health -> upload -> FFmpeg -> VAD -> Whisper")
    print("                  -> WhisperX alignment -> pyannote diarization")

    with TestClient(app) as client:
        # --- Stage 0: health ---
        hr("STAGE 0  Health check (server + GPU)")
        h = client.get("/health").json()
        print(f"  status         : {h['status']}")
        print(f"  python         : {h['python_version']}")
        print(f"  CUDA available : {h['cuda_available']}")
        print(f"  GPU            : {h['gpu_name']}")
        print(f"  VRAM total     : {h['vram_total_mb']} MB")

        # --- Stage 1: upload ---
        hr("STAGE 1  Upload + kick off processing")
        t0 = time.time()
        with audio_path.open("rb") as f:
            r = client.post(
                "/meetings",
                files={"file": (audio_path.name, f, "application/octet-stream")},
                data={"title": "Verification run", "primary_language": "auto"},
            )
        assert r.status_code == 201, r.text
        m = r.json()
        meeting_id = m["id"]
        print(f"  HTTP status        : {r.status_code} (created)")
        print(f"  meeting_id         : {meeting_id}")
        print(f"  original_filename  : {m['original_filename']}")
        print(f"  primary_language   : {m['primary_language']}")
        print(f"  expected_speakers  : {m['expected_speakers']} (None = auto-detect)")

        # --- Wait for the pipeline to finish ---
        hr("PIPELINE  Processing (watch the status advance)")
        prev = None
        for _ in range(600):
            s = client.get(f"/meetings/{meeting_id}").json()["status"]
            if s != prev:
                print(f"  [{time.time() - t0:6.1f}s] status -> {s}")
            prev = s
            if s == "diarized":
                break
            if s.startswith("error"):
                print(f"\n  PIPELINE FAILED: {s}")
                return 1
            time.sleep(1)
        else:
            print("  TIMEOUT after 600s")
            return 1
        elapsed = time.time() - t0

        meeting = client.get(f"/meetings/{meeting_id}").json()
        t = client.get(f"/meetings/{meeting_id}/transcript").json()

        # --- Stage 2: audio + VAD ---
        hr("STAGE 2  Audio extraction + Voice Activity Detection")
        meeting_dir = Path("./data/uploads") / str(meeting_id)
        print(f"  duration_seconds   : {meeting['duration_seconds']}")
        print(f"  normalized WAV     : {(meeting_dir / 'audio.wav').exists()}")
        print(f"  VAD sidecar JSON   : {(meeting_dir / 'vad_segments.json').exists()}")
        import json as _json
        vad = _json.loads((meeting_dir / "vad_segments.json").read_text())
        print(f"  speech regions     : {len(vad)}")
        for v in vad[:5]:
            print(f"      {v['start']:6.2f} - {v['end']:6.2f} s")

        # --- Stage 3: transcription ---
        hr("STAGE 3  Transcription (Faster-Whisper)")
        print(f"  detected_language  : {t['detected_language']}")
        print(f"  segment_count      : {t['segment_count']}")

        # --- Stage 5: speakers ---
        hr("STAGE 5  Speaker diarization (pyannote)")
        print(f"  speakers detected  : {len(t['speakers'])}")
        for sp in t["speakers"]:
            print(f"      id={sp['id']}  label={sp['label']!r}  name={sp['display_name']}")

        # --- Combined transcript view ---
        hr("RESULT  Diarized transcript with word-level timing (Phase 4)")
        spk_label = {sp["id"]: sp["label"] for sp in t["speakers"]}
        for seg in t["segments"]:
            who = spk_label.get(seg["speaker_id"], "unknown")
            print(f"\n  [{seg['start_seconds']:5.2f}-{seg['end_seconds']:5.2f}s] "
                  f"{who}: {seg['text']!r}")
            wl = seg["words"]
            if wl:
                preview = "  ".join(
                    f"{w['text']}({w['start_seconds']:.2f})" for w in wl[:8]
                )
                print(f"        words: {preview}" + (" ..." if len(wl) > 8 else ""))

        # --- Summary ---
        hr("SUMMARY")
        print(f"  Total processing time : {elapsed:.1f}s for {meeting['duration_seconds']}s of audio")
        print(f"  Final status          : {meeting['status']}")
        print(f"  Speakers              : {len(t['speakers'])}")
        print(f"  Segments              : {t['segment_count']}")
        total_words = sum(len(s["words"]) for s in t["segments"])
        print(f"  Words (timed)         : {total_words}")
        print("\n  All stages completed successfully.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
