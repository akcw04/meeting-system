"""Delete meeting(s) and ALL their data - DB rows + uploaded/derived files.

A meeting lives in three places; this removes all of them so nothing is
orphaned (a stale DB row would still show in the dashboard):
  - database rows   (meetings + speakers + segments + words + the 5 insight tables)
  - data/uploads/<id>/   (original video + audio.wav + any sidecars)
  - data/outputs/<id>/   (exported .docx)

USAGE
  python tests\\delete_meeting.py --list            # show everything, with disk sizes
  python tests\\delete_meeting.py 1 2 3 4           # delete meetings 1,2,3,4

SAFETY: only deletes the ids you pass. Stop the backend first (avoids a DB lock
and avoids deleting a meeting mid-processing).
"""
import shutil
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB = BACKEND / "data" / "app.db"
UPLOADS = BACKEND / "data" / "uploads"
OUTPUTS = BACKEND / "data" / "outputs"


def dir_size_mb(p: Path) -> float:
    if not p.exists():
        return 0.0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def list_meetings(con: sqlite3.Connection) -> None:
    rows = con.execute(
        "SELECT id, title, status, duration_seconds FROM meetings ORDER BY id"
    ).fetchall()
    print(f"{'id':>4}  {'disk MB':>8}  {'dur(s)':>8}  status | title")
    print("-" * 78)
    total = 0.0
    for r in rows:
        mb = dir_size_mb(UPLOADS / str(r["id"])) + dir_size_mb(OUTPUTS / str(r["id"]))
        total += mb
        dur = r["duration_seconds"]
        dur_s = f"{dur:.0f}" if dur is not None else "-"
        print(f"{r['id']:>4}  {mb:>8.1f}  {dur_s:>8}  {(r['status'] or '')[:34]:34} | {r['title']}")
    print("-" * 78)
    print(f"{len(rows)} meetings, {total:.1f} MB on disk (uploads + outputs)")


def delete_one(con: sqlite3.Connection, mid: int) -> str:
    row = con.execute("SELECT title FROM meetings WHERE id = ?", (mid,)).fetchone()
    con.execute("PRAGMA foreign_keys = ON")
    # child-first, FK-safe order
    con.execute(
        "DELETE FROM words WHERE segment_id IN (SELECT id FROM segments WHERE meeting_id = ?)",
        (mid,),
    )
    for t in ("action_items", "decisions", "deadlines", "issues", "risks"):
        con.execute(f"DELETE FROM {t} WHERE meeting_id = ?", (mid,))
    con.execute("DELETE FROM segments WHERE meeting_id = ?", (mid,))
    con.execute("DELETE FROM speakers WHERE meeting_id = ?", (mid,))
    con.execute("DELETE FROM meetings WHERE id = ?", (mid,))
    for base in (UPLOADS, OUTPUTS):
        d = base / str(mid)
        if d.exists():
            shutil.rmtree(d)
    return row["title"] if row else "(no DB row - files only)"


def main() -> int:
    if not DB.exists():
        print(f"DB not found at {DB}")
        return 1
    args = sys.argv[1:]
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        if not args or args[0] == "--list":
            list_meetings(con)
            return 0
        try:
            ids = [int(a) for a in args]
        except ValueError:
            print("Pass meeting ids (integers) or --list.")
            return 2
        for mid in ids:
            title = delete_one(con, mid)
            print(f"deleted meeting {mid}: {title}")
        con.commit()
        print(f"\nDone - removed {len(ids)} meeting(s).")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
