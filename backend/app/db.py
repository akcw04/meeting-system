"""SQLite database - schema, connection helper, idempotent migrations.

SQLite is a single file on disk (no separate server). We open one
connection per request, run the work, then close it.

The schema captures:
  meetings  -> one row per uploaded recording
  speakers  -> per-meeting speaker labels (user-renameable in UI)
  segments  -> diarized transcript chunks (start, end, speaker, text, language)
  action_items, decisions, deadlines, issues, risks
            -> extracted insights, each linked back to a segment so we
               can prove the LLM didn't hallucinate.
  templates -> user-uploaded Word export templates (IR §2.2.7); the
               .docx file itself lives in data/templates/<id>.docx.

Migrations: `init_db()` runs the schema (CREATE TABLE IF NOT EXISTS) AND
any column-add migrations needed to bring an older DB up to current.
"""
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    duration_seconds REAL,
    language TEXT,                     -- output language of the transcript ('en' if translated)
    languages_detected TEXT,           -- codes detected across the audio, e.g. "en,zh" (code-switch flag)
    primary_language TEXT NOT NULL DEFAULT 'auto',  -- user's preferred OUTPUT language: 'auto', 'en', 'zh'
    expected_speakers INTEGER,         -- NULL = auto-detect (default)
    status TEXT NOT NULL DEFAULT 'uploaded',
    progress REAL,                     -- 0..1 within the current long stage (transcribe / categorize)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    display_name TEXT,
    UNIQUE(meeting_id, label)
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    speaker_id INTEGER REFERENCES speakers(id),
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    text TEXT NOT NULL,
    language TEXT
);

CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    text TEXT NOT NULL,
    score REAL
);

CREATE INDEX IF NOT EXISTS idx_words_segment ON words(segment_id);
CREATE INDEX IF NOT EXISTS idx_words_start ON words(start_seconds);

CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    owner TEXT,
    due_date TEXT,
    source_segment_id INTEGER REFERENCES segments(id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    source_segment_id INTEGER REFERENCES segments(id)
);

CREATE TABLE IF NOT EXISTS deadlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    target_date TEXT,
    source_segment_id INTEGER REFERENCES segments(id)
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    severity TEXT,
    source_segment_id INTEGER REFERENCES segments(id)
);

CREATE TABLE IF NOT EXISTS risks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    mitigation TEXT,
    source_segment_id INTEGER REFERENCES segments(id)
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                -- user-facing label, e.g. "Acme Corp minutes"
    original_filename TEXT NOT NULL,   -- the .docx name as uploaded
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent migrations for DBs created by earlier phases."""
    # Phase 3: add primary_language to meetings if missing.
    if not _has_column(conn, "meetings", "primary_language"):
        conn.execute(
            "ALTER TABLE meetings ADD COLUMN primary_language TEXT NOT NULL DEFAULT 'auto'"
        )
    # Phase 5: add expected_speakers hint (NULL = let pyannote auto-detect).
    if not _has_column(conn, "meetings", "expected_speakers"):
        conn.execute(
            "ALTER TABLE meetings ADD COLUMN expected_speakers INTEGER"
        )
    # Phase 7: overall LLM-written meeting summary (<=300 words).
    if not _has_column(conn, "meetings", "summary"):
        conn.execute("ALTER TABLE meetings ADD COLUMN summary TEXT")
    # Session 7: live progress (0..1) within the current long stage.
    if not _has_column(conn, "meetings", "progress"):
        conn.execute("ALTER TABLE meetings ADD COLUMN progress REAL")
    # Session 10 (2026-06-20): languages detected across the audio (code-switch
    # routing + UI notice), e.g. "en,zh".
    if not _has_column(conn, "meetings", "languages_detected"):
        conn.execute("ALTER TABLE meetings ADD COLUMN languages_detected TEXT")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    settings.ensure_dirs()
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _run_migrations(conn)
