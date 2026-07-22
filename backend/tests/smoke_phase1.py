"""Phase 1 smoke test — run after writing all Phase 1 files.

Verifies:
  1. The FastAPI app boots (lifespan runs ensure_dirs + init_db).
  2. /health responds with GPU info.
  3. SQLite tables are created.
  4. Data directories exist.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from app.main import app


def main() -> int:
    print("=== Phase 1 smoke test ===")
    with TestClient(app) as client:
        print("App started; lifespan ran (ensure_dirs + init_db).")

        r = client.get("/health")
        print(f"\nGET /health -> {r.status_code}")
        print(json.dumps(r.json(), indent=2))
        assert r.status_code == 200, "Health check should return 200"
        assert r.json()["status"] == "ok", "Status should be 'ok'"
        assert r.json()["cuda_available"] is True, "CUDA should be available"

    conn = sqlite3.connect("./data/app.db")
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    conn.close()
    print(f"\nSQLite tables: {tables}")
    expected = {
        "action_items",
        "decisions",
        "deadlines",
        "issues",
        "meetings",
        "risks",
        "segments",
        "speakers",
    }
    assert set(tables) >= expected, f"Missing tables: {expected - set(tables)}"

    print("\nData directories:")
    for d in ("./data", "./data/uploads", "./data/outputs", "./data/models"):
        exists = Path(d).exists()
        print(f"  {d}: {'exists' if exists else 'MISSING'}")
        assert exists, f"Directory {d} should exist"

    print("\nSmoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
