"""FastAPI app entrypoint.

Run with:
    cd backend
    .\\.venv\\Scripts\\activate
    uvicorn app.main:app --host 127.0.0.1 --port 8000

Then open http://127.0.0.1:8000/docs for the interactive API docs.

NOTE: do NOT add --reload while a meeting is processing - a file-change
restart kills in-flight background tasks (see docs/BUGS.md #12 and #19).
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.config import settings
from app.db import get_conn, init_db
from app.routes import health, meetings, templates

# Statuses that mean "a background task is working on this right now".
# Background tasks die with the server process, so any meeting still in
# one of these states at STARTUP is orphaned and must be made retry-able.
_BUSY_PIPELINE = ("uploading", "uploaded", "audio_extracted", "vad_done",
                  "transcribing", "aligning", "diarizing")


def _sweep_orphaned_meetings() -> None:
    with get_conn() as conn:
        # Mid-pipeline orphans: transcript incomplete -> hard error, re-upload
        # (the stored audio in data/uploads can be reused to resume manually).
        placeholders = ",".join("?" * len(_BUSY_PIPELINE))
        n1 = conn.execute(
            f"UPDATE meetings SET status = 'error: interrupted by server restart', "
            f"updated_at = CURRENT_TIMESTAMP WHERE status IN ({placeholders})",
            _BUSY_PIPELINE,
        ).rowcount
        # Categorize orphans: transcript IS safe - mark retry-able so the
        # dashboard shows the Retry button.
        n2 = conn.execute(
            "UPDATE meetings SET status = 'categorize_failed: interrupted by server restart', "
            "updated_at = CURRENT_TIMESTAMP WHERE status = 'categorizing'"
        ).rowcount
    if n1 or n2:
        print(f"[startup] swept orphaned meetings: {n1} mid-pipeline, {n2} mid-categorize")


# Personal + system intro printed to the console on startup (FYP requirement:
# the program must begin with a personal and system introduction).
_STARTUP_BANNER = r"""
============================================================
  MULTILINGUAL MEETING TRANSCRIPTION & CONTENT
  CATEGORIZATION SYSTEM
------------------------------------------------------------
  Author      : Annie Kiu Chi Wen (TP070557)
  Programme   : BSc (Hons) Software Engineering, APU
  Description : A fully-local AI system that transcribes,
                diarizes and categorizes code-switched
                (English / Bahasa Melayu / Mandarin)
                meeting recordings.
============================================================
"""


def _print_startup_banner() -> None:
    """Print the personal + system intro banner to the console."""
    print(_STARTUP_BANNER)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _print_startup_banner()
    settings.ensure_dirs()
    init_db()
    _sweep_orphaned_meetings()
    yield


app = FastAPI(
    title="Kairos API",
    description="Local AI multilingual meeting transcription & content categorization",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(meetings.router)
app.include_router(templates.router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Bare root URL redirects to the interactive API docs."""
    return RedirectResponse(url="/docs")
