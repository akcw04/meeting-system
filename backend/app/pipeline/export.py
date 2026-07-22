"""Word document export (Phase 8).

A meeting becomes a .docx two ways:
  - the built-in default template (templates/default.docx, docxtpl {{ tags }}),
    rendered by export_docx();
  - a USER's own document, filled WITHOUT any code-like syntax by
    pipeline/fill_template.py (plain section headings or friendly [[markers]]).

Both share build_meeting_context() for data shaping - this module owns the
data, the template owns the formatting (the IR's Separation-of-Concerns design).

Output: data/outputs/<meeting_id>/<slug>_minutes.docx
Re-rendered on every export request so transcript/insight edits are reflected.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from docxtpl import DocxTemplate

from app.config import settings
from app.db import get_conn

TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "templates" / "default.docx"

LANGUAGE_NAMES = {"en": "English", "zh": "Chinese"}


class TemplateRenderError(ValueError):
    """A Word document could not be produced/filled with a meeting's data.

    Raised for the built-in template (bad Jinja) or a user document we cannot
    open as a .docx. Subclasses ValueError so routes can catch it specifically
    and return a user-fixable 400 instead of a 409.
    """


def _fmt_time(seconds: float | None) -> str:
    s = int(seconds or 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _slug(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:max_len] or "meeting"


def build_meeting_context(meeting_id: int) -> dict:
    """Shape a meeting's DB rows into the render context shared by the built-in
    docxtpl template AND the user-document fill engine.

    Raises ValueError if the meeting is missing or has no transcript yet.
    """
    with get_conn() as conn:
        meeting = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if meeting is None:
            raise ValueError(f"Meeting {meeting_id} not found")

        speakers = conn.execute(
            "SELECT * FROM speakers WHERE meeting_id = ? ORDER BY id", (meeting_id,)
        ).fetchall()
        segments = conn.execute(
            "SELECT * FROM segments WHERE meeting_id = ? ORDER BY start_seconds",
            (meeting_id,),
        ).fetchall()

        def rows(table: str) -> list[dict]:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM {table} WHERE meeting_id = ? ORDER BY id",
                    (meeting_id,),
                ).fetchall()
            ]

        action_items = rows("action_items")
        decisions = rows("decisions")
        deadlines = rows("deadlines")
        issues = rows("issues")
        risks = rows("risks")

    if not segments:
        raise ValueError(
            f"Meeting {meeting_id} has no transcript yet (status: {meeting['status']})"
        )

    label_of = {s["id"]: (s["display_name"] or s["label"]) for s in speakers}
    time_of = {s["id"]: _fmt_time(s["start_seconds"]) for s in segments}  # for source refs

    def _src(row: dict) -> str | None:
        return time_of.get(row.get("source_segment_id"))

    return {
        "meeting": {
            "title": meeting["title"],
            "date": (meeting["created_at"] or "")[:10],
            "duration": _fmt_time(meeting["duration_seconds"]),
            "language_name": LANGUAGE_NAMES.get(meeting["language"], meeting["language"] or "-"),
            "speaker_count": len(speakers),
        },
        "attendees": [label_of[s["id"]] for s in speakers],
        "summary": meeting["summary"] or "No summary generated yet.",
        "action_items": [
            {"description": a["description"], "owner": a["owner"], "due": a["due_date"], "source": _src(a)}
            for a in action_items
        ],
        "decisions": [{"description": d["description"], "source": _src(d)} for d in decisions],
        "deadlines": [
            {"description": d["description"], "date": d["target_date"], "source": _src(d)} for d in deadlines
        ],
        "issues": [{"description": i["description"], "source": _src(i)} for i in issues],
        "risks": [
            {"description": r["description"], "mitigation": r["mitigation"], "source": _src(r)} for r in risks
        ],
        "transcript": [
            {
                "time": _fmt_time(seg["start_seconds"]),
                "speaker": label_of.get(seg["speaker_id"], "Unknown"),
                "text": seg["text"].strip(),
            }
            for seg in segments
        ],
        "generated_on": datetime.now().strftime("%d %B %Y, %H:%M"),
    }


def export_docx(meeting_id: int, template_path: Path | None = None) -> Path:
    """Render the meeting minutes via the built-in docxtpl template and return
    the output path. (User-uploaded documents go through fill_template.py.)
    """
    template = Path(template_path) if template_path else TEMPLATE_PATH
    if not template.is_file():
        raise FileNotFoundError(
            f"Template not found at {template}. Regenerate it with: "
            f"python templates\\generate_default_template.py"
        )

    context = build_meeting_context(meeting_id)
    doc = DocxTemplate(str(template))
    try:
        doc.render(context)
    except Exception as exc:  # malformed Jinja in the built-in template
        raise TemplateRenderError(
            f"Could not render the built-in template ({type(exc).__name__}: {exc})."
        ) from exc

    out_dir = settings.output_dir / str(meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_slug(context['meeting']['title'])}_minutes.docx"
    doc.save(str(out_path))
    return out_path
