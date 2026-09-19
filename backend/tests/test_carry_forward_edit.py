"""Smoke suite: carry-forward edit / delete (human-in-the-loop correction).

Runs against a THROWAWAY database in the scratchpad - it never opens the real
data/app.db. Verifies the happy paths, every rejection path, and (the point of
"do not affect other parts") that no other table is touched.
"""
import os, sys, tempfile, shutil
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="kairos_cf_test_"))
os.environ["DATA_DIR"] = str(SCRATCH)
os.environ["DB_PATH"] = str(SCRATCH / "app.db")
os.environ["UPLOAD_DIR"] = str(SCRATCH / "uploads")
os.environ["OUTPUT_DIR"] = str(SCRATCH / "outputs")

BACKEND = Path(__file__).resolve().parents[1]   # backend/
sys.path.insert(0, str(BACKEND))
os.chdir(SCRATCH)

from fastapi.testclient import TestClient          # noqa: E402
from app.config import settings                    # noqa: E402
from app.db import get_conn, init_db               # noqa: E402
from app.main import app                           # noqa: E402

REAL_DB = BACKEND / "data" / "app.db"
assert Path(settings.db_path).resolve() != REAL_DB.resolve(), "refusing to touch the real DB"
real_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

init_db()
c = TestClient(app)
ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {extra}")


def counts():
    with get_conn() as conn:
        out = {}
        for t in ("meetings", "segments", "action_items", "decisions",
                  "deadlines", "issues", "risks", "carry_forward"):
            out[t] = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        return out


# ---- seed: meeting A (with an action), meeting B follows it ----
with get_conn() as conn:
    conn.execute("INSERT INTO meetings (id, title, original_filename, audio_path, status) "
                 "VALUES (1, 'Sprint 1', 'a.mp3', '/tmp/a.wav', 'done')")
    conn.execute("INSERT INTO meetings (id, title, original_filename, audio_path, status,"
                 " follow_up_of, carry_forward_status) "
                 "VALUES (2, 'Sprint 2', 'b.mp3', '/tmp/b.wav', 'done', 1, 'ready')")
    conn.execute("INSERT INTO action_items (id, meeting_id, description, owner) "
                 "VALUES (10, 1, 'Ship the installer', 'Wei')")
    conn.execute("INSERT INTO segments (id, meeting_id, start_seconds, end_seconds, text) "
                 "VALUES (100, 2, 0, 5, 'The installer went out on Tuesday.')")
    # two carry-forward rows on meeting 2
    conn.execute("INSERT INTO carry_forward (id, meeting_id, previous_meeting_id, previous_action_id,"
                 " description, owner, status, note, source_segment_id) "
                 "VALUES (200, 2, 1, 10, 'Ship the installer', 'Wei', 'in_progress', 'still going', 100)")
    conn.execute("INSERT INTO carry_forward (id, meeting_id, previous_meeting_id, previous_action_id,"
                 " description, owner, status, note, source_segment_id) "
                 "VALUES (201, 2, 1, NULL, 'Write the README', NULL, 'not_discussed', NULL, NULL)")

before = counts()
print("\n--- PATCH ---")

r = c.patch("/meetings/2/carry-forward/200", json={"status": "completed"})
check("overrule verdict in_progress -> completed", r.status_code == 200 and r.json()["status"] == "completed", r.text[:120])
check("citation is preserved by an edit", r.json().get("source_segment_id") == 100, r.text[:120])

r = c.patch("/meetings/2/carry-forward/200",
            json={"description": "Ship the Windows installer", "note": "confirmed by Wei in the room"})
check("edit wording and note", r.status_code == 200 and r.json()["description"] == "Ship the Windows installer", r.text[:120])
check("note written", r.json()["note"] == "confirmed by Wei in the room", r.text[:120])
check("status untouched when not supplied", r.json()["status"] == "completed", r.text[:120])

r = c.patch("/meetings/2/carry-forward/201", json={"status": "In Progress"})
check("status is normalised ('In Progress' -> in_progress)", r.status_code == 200 and r.json()["status"] == "in_progress", r.text[:120])

r = c.patch("/meetings/2/carry-forward/200", json={"status": "finished"})
check("invalid status rejected", r.status_code == 400, r.text[:120])

r = c.patch("/meetings/2/carry-forward/200", json={"description": "   "})
check("empty description rejected", r.status_code == 400, r.text[:120])

r = c.patch("/meetings/2/carry-forward/200", json={})
check("empty body rejected", r.status_code == 400, r.text[:120])

r = c.patch("/meetings/2/carry-forward/999", json={"status": "blocked"})
check("unknown item -> 404", r.status_code == 404, r.text[:120])

r = c.patch("/meetings/1/carry-forward/200", json={"status": "blocked"})
check("item from another meeting -> 404", r.status_code == 404, r.text[:120])

r = c.patch("/meetings/2/carry-forward/200", json={"source_segment_id": 999})
check("unknown field ignored, not written", r.status_code == 400, r.text[:120])

print("\n--- GET still consistent ---")
r = c.get("/meetings/2/carry-forward")
body = r.json()
check("GET returns both rows", r.status_code == 200 and len(body["items"]) == 2, r.text[:120])
check("GET reflects the edit", body["items"][0]["description"] == "Ship the Windows installer", r.text[:160])

print("\n--- DELETE ---")
r = c.delete("/meetings/2/carry-forward/201")
check("delete returns 204", r.status_code == 204, r.text[:120])
r = c.delete("/meetings/2/carry-forward/201")
check("deleting twice -> 404", r.status_code == 404, r.text[:120])
r = c.delete("/meetings/1/carry-forward/200")
check("delete from wrong meeting -> 404", r.status_code == 404, r.text[:120])
r = c.get("/meetings/2/carry-forward")
check("one row left after delete", len(r.json()["items"]) == 1, r.text[:160])

print("\n--- nothing else touched ---")
after = counts()
expected = dict(before); expected["carry_forward"] = before["carry_forward"] - 1
for t in before:
    check(f"table '{t}' {before[t]} -> {after[t]}", after[t] == expected[t],
          f"expected {expected[t]}")

if real_mtime is not None:
    check("real data/app.db not modified", REAL_DB.stat().st_mtime == real_mtime)

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
