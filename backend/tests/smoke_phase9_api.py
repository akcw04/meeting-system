"""Phase 9a smoke test - the dashboard's backend data layer.

Covers the six endpoints the React UI will consume:
  GET   /meetings/{id}/segments?offset&limit     (paged transcript)
  GET   /meetings/{id}/segments/{sid}/words      (lazy word timing)
  PATCH /meetings/{id}/segments/{sid}            (edit text / reassign speaker)
  PATCH /meetings/{id}/speakers/{spk}            (rename speaker)
  GET   /meetings/{id}/search?q=                 (keyword search with scroll index)
  GET   /meetings/{id}/audio                     (WAV streaming for the player)

Self-contained: plants a synthetic meeting (with a real 1-second WAV).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from app.db import get_conn, init_db  # noqa: E402


def plant_meeting() -> tuple[int, list[int], dict[str, int]]:
    init_db()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO meetings (title, original_filename, audio_path, duration_seconds, "
            "language, primary_language, status) "
            "VALUES ('Phase 9 API Test', 'fake.mp4', '', 30.0, 'en', 'en', 'diarized')"
        )
        mid = cur.lastrowid

        # Real tiny WAV so the audio route has something to stream.
        audio_dir = Path(f"./data/uploads/{mid}")
        audio_dir.mkdir(parents=True, exist_ok=True)
        wav_path = audio_dir / "audio.wav"
        sf.write(str(wav_path), np.zeros(16000, dtype="float32"), 16000)
        conn.execute(
            "UPDATE meetings SET audio_path = ? WHERE id = ?", (str(wav_path), mid)
        )

        spk = {}
        for label in ("Speaker 1", "Speaker 2"):
            c = conn.execute(
                "INSERT INTO speakers (meeting_id, label) VALUES (?, ?)", (mid, label)
            )
            spk[label] = c.lastrowid

        texts = [
            "Welcome everyone to the planning meeting.",
            "The budget review is scheduled for next week.",
            "We must finalize the venue booking soon.",
            "Catering options need a decision by Friday.",
            "Marketing assets are 50 percent done.",
            "The budget cap is twenty thousand ringgit.",
        ]
        seg_ids = []
        for i, text in enumerate(texts):
            c = conn.execute(
                "INSERT INTO segments (meeting_id, speaker_id, start_seconds, end_seconds, text, language) "
                "VALUES (?, ?, ?, ?, ?, 'en')",
                (mid, spk["Speaker 1"] if i % 2 == 0 else spk["Speaker 2"],
                 i * 5.0, i * 5.0 + 4.5, text),
            )
            seg_ids.append(c.lastrowid)

        # words for the first segment only (lazy-load test)
        for j, w in enumerate(["Welcome", "everyone", "to", "the", "planning", "meeting."]):
            conn.execute(
                "INSERT INTO words (segment_id, start_seconds, end_seconds, text, score) "
                "VALUES (?, ?, ?, ?, ?)",
                (seg_ids[0], j * 0.5, j * 0.5 + 0.45, w, 0.9 - j * 0.1),
            )
    return mid, seg_ids, spk


def main() -> int:
    print("=== Phase 9a smoke test: dashboard API ===")
    mid, seg_ids, spk = plant_meeting()
    print(f"Planted meeting id={mid} with {len(seg_ids)} segments")

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        # --- paged segments ---
        r = client.get(f"/meetings/{mid}/segments?offset=0&limit=4")
        assert r.status_code == 200, r.text
        page = r.json()
        assert page["total"] == 6 and len(page["segments"]) == 4
        r2 = client.get(f"/meetings/{mid}/segments?offset=4&limit=4")
        assert len(r2.json()["segments"]) == 2
        print("paged segments        OK (total=6, slice sizes 4+2)")

        # --- lazy words ---
        r = client.get(f"/meetings/{mid}/segments/{seg_ids[0]}/words")
        assert r.status_code == 200 and len(r.json()) == 6
        r = client.get(f"/meetings/{mid}/segments/{seg_ids[1]}/words")
        assert r.status_code == 200 and r.json() == []
        print("lazy word fetch       OK (6 words seg1, 0 words seg2)")

        # --- edit segment text ---
        r = client.patch(
            f"/meetings/{mid}/segments/{seg_ids[0]}",
            json={"text": "Welcome everybody to the project planning meeting."},
        )
        assert r.status_code == 200 and "everybody" in r.json()["text"]
        print("segment text edit     OK")

        # --- reassign speaker ---
        r = client.patch(
            f"/meetings/{mid}/segments/{seg_ids[1]}",
            json={"speaker_id": spk["Speaker 1"]},
        )
        assert r.status_code == 200 and r.json()["speaker_id"] == spk["Speaker 1"]
        # invalid speaker -> 409
        r = client.patch(
            f"/meetings/{mid}/segments/{seg_ids[1]}", json={"speaker_id": 999999}
        )
        assert r.status_code == 409
        print("speaker reassignment  OK (+409 on foreign speaker)")

        # --- rename speaker ---
        r = client.patch(
            f"/meetings/{mid}/speakers/{spk['Speaker 2']}",
            json={"display_name": "Ms. Lai"},
        )
        assert r.status_code == 200 and r.json()["display_name"] == "Ms. Lai"
        print("speaker rename        OK ('Ms. Lai')")

        # --- search ---
        r = client.get(f"/meetings/{mid}/search", params={"q": "budget"})
        assert r.status_code == 200, r.text
        hits = r.json()
        assert len(hits) == 2, f"expected 2 budget hits, got {len(hits)}"
        assert hits[0]["index"] == 1 and hits[1]["index"] == 5, hits
        assert "budget" in hits[0]["snippet"].lower()
        # short query rejected
        assert client.get(f"/meetings/{mid}/search", params={"q": "a"}).status_code == 400
        print(f"search                OK (2 hits at indexes {hits[0]['index']},{hits[1]['index']})")

        # --- audio streaming ---
        r = client.get(f"/meetings/{mid}/audio")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
        assert len(r.content) > 30000  # 1s of 16-bit 16kHz mono ~ 32KB
        # Range request (seek support for the player)
        r = client.get(f"/meetings/{mid}/audio", headers={"Range": "bytes=0-999"})
        print(f"audio streaming       OK (full=200, range request -> {r.status_code})")

    print("\nPhase 9a smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
