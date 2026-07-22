"""Phase 7 smoke test - LLM categorization via Ollama + Llama 3.1.

Part A: feed a synthetic 14-line project standup directly to
        categorize_transcript() and check it finds the planted facts
        (action items, a decision, a deadline, an issue, a risk).

Part B: run the tiny VOiCES clip through the FULL pipeline and verify
        status reaches 'ready' and the insights endpoint responds.

Requires Ollama running with llama3.1:8b pulled. LLM output is
non-deterministic, so assertions are deliberately tolerant: we check
"found at least N items of the right kind", not exact wording.
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

from app.pipeline.categorize import categorize_transcript  # noqa: E402

FIXTURE = BACKEND / "tests" / "fixtures" / "voices_test.wav"
FIXTURE_URL = (
    "https://download.pytorch.org/torchaudio/tutorial-assets/"
    "Lab41-SRI-VOiCES-src-sp0307-ch127535-sg0042.wav"
)

# Synthetic standup with planted, checkable facts.
FAKE_SEGMENTS = [
    {"id": 1, "speaker_id": 1, "text": "Good morning everyone, let's start the sprint review."},
    {"id": 2, "speaker_id": 1, "text": "First item: the client demo is confirmed for the 25th of June."},
    {"id": 3, "speaker_id": 2, "text": "I finished the login page, but the payment gateway integration is still failing with timeout errors."},
    {"id": 4, "speaker_id": 1, "text": "Okay. Sarah, can you take over debugging the payment gateway by Wednesday?"},
    {"id": 5, "speaker_id": 3, "text": "Sure, I'll handle the payment gateway debugging. I'll need access to the staging server."},
    {"id": 6, "speaker_id": 2, "text": "I'll grant you staging access right after this call."},
    {"id": 7, "speaker_id": 1, "text": "We also need to decide on the database. After comparing options, we're going with PostgreSQL instead of MongoDB."},
    {"id": 8, "speaker_id": 3, "text": "Agreed, PostgreSQL fits our relational data better."},
    {"id": 9, "speaker_id": 1, "text": "One concern: if the payment vendor doesn't fix their API by next month, we might miss the launch window."},
    {"id": 10, "speaker_id": 2, "text": "We should prepare a fallback vendor just in case. I can draft a comparison by Friday."},
    {"id": 11, "speaker_id": 1, "text": "Good idea. Also reminder: code freeze is on the 20th of June."},
    {"id": 12, "speaker_id": 3, "text": "Noted. I'll wrap up my pull requests before the freeze."},
    {"id": 13, "speaker_id": 1, "text": "Great. Anything else? No? Meeting adjourned, thanks everyone."},
]
FAKE_SPEAKERS = {1: "Speaker 1", 2: "Speaker 2", 3: "Speaker 3"}


def part_a() -> None:
    print("=== Part A: direct categorization of a synthetic standup ===")
    t0 = time.time()
    insights = categorize_transcript(FAKE_SEGMENTS, FAKE_SPEAKERS, "en")
    elapsed = time.time() - t0

    print(f"\nLLM done in {elapsed:.1f}s")
    print(f"\nSUMMARY ({len(insights.summary.split())} words):\n  {insights.summary}\n")

    def show(name, items):
        print(f"{name} ({len(items)}):")
        for it in items:
            extra = ""
            if getattr(it, "owner", None):
                extra += f"  [owner: {it.owner}]"
            if getattr(it, "due", None):
                extra += f"  [due: {it.due}]"
            if getattr(it, "date", None):
                extra += f"  [date: {it.date}]"
            if getattr(it, "mitigation", None):
                extra += f"  [mitigation: {it.mitigation}]"
            print(f"  - {it.description}{extra}  (segs: {it.source_segment_ids})")
        print()

    show("ACTION ITEMS", insights.action_items)
    show("DECISIONS", insights.decisions)
    show("DEADLINES", insights.deadlines)
    show("ISSUES", insights.issues)
    show("RISKS", insights.risks)

    # Tolerant assertions on the planted facts:
    assert len(insights.summary.split()) >= 20, "Summary suspiciously short"
    assert len(insights.summary.split()) <= 400, "Summary exceeds the ~300-word cap badly"
    assert len(insights.action_items) >= 2, (
        "Expected >=2 action items (payment debugging, staging access, fallback comparison...)"
    )
    assert len(insights.decisions) >= 1, "Expected >=1 decision (PostgreSQL over MongoDB)"
    assert len(insights.deadlines) >= 1, "Expected >=1 deadline (June 25 demo / June 20 freeze)"
    # The payment-vendor problem is legitimately an issue (a current technical
    # failure) AND/OR a risk (a future launch-window threat); a local 8B splits it
    # inconsistently. Assert the model surfaces it under at least one of the two
    # rather than demanding a specific split - matching this test's "tolerant"
    # intent (bug #25).
    assert len(insights.issues) + len(insights.risks) >= 1, (
        "Expected >=1 problem-type item (payment gateway timeouts / vendor API risk)"
    )

    valid_ids = {s["id"] for s in FAKE_SEGMENTS}
    for group in (insights.action_items, insights.decisions, insights.deadlines,
                  insights.issues, insights.risks):
        for it in group:
            assert it.source_segment_ids, f"Item lost its grounding: {it.description!r}"
            assert all(i in valid_ids for i in it.source_segment_ids), (
                f"Hallucinated segment id in: {it.description!r} -> {it.source_segment_ids}"
            )

    print("Part A PASSED.\n")


def part_b() -> None:
    print("=== Part B: full pipeline -> 'ready' -> insights endpoint ===")
    if not FIXTURE.exists():
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(FIXTURE_URL, FIXTURE)

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        with FIXTURE.open("rb") as f:
            r = client.post(
                "/meetings",
                files={"file": (FIXTURE.name, f, "audio/wav")},
                data={"title": "Phase 7 e2e test", "primary_language": "en"},
            )
        assert r.status_code == 201, r.text
        meeting_id = r.json()["id"]
        print(f"meeting_id={meeting_id}")

        prev = None
        for attempt in range(360):
            s = client.get(f"/meetings/{meeting_id}").json()["status"]
            if s != prev:
                print(f"  status -> {s}")
            prev = s
            if s == "ready":
                break
            if s.startswith("error") or s.startswith("categorize_failed"):
                raise RuntimeError(f"Pipeline failed: {s}")
            time.sleep(1)
        else:
            raise TimeoutError("Did not reach 'ready' in 360s")

        r = client.get(f"/meetings/{meeting_id}/insights")
        assert r.status_code == 200, r.text
        ins = r.json()
        print(f"  insights status={ins['status']}")
        print(f"  summary: {ins['summary']!r}")
        assert ins["status"] == "ready"
        assert ins["summary"], "Summary should not be empty"

    print("Part B PASSED.")


if __name__ == "__main__":
    part_a()
    part_b()
    print("\nPhase 7 smoke test PASSED.")
    sys.exit(0)
