"""Meeting routes - upload, read, list, transcript, follow-up linking, export.

POST  /meetings                     -> upload an audio/video, kick off full pipeline
GET   /meetings                     -> list all meetings (newest first)
GET   /meetings/{id}                -> fetch one meeting's current state
GET   /meetings/{id}/transcript     -> fetch all transcript segments for a meeting
PATCH /meetings/{id}/follow-up      -> link/unlink the meeting this one follows up
GET   /meetings/{id}/carry-forward  -> progress on the previous meeting's actions
POST  /meetings/{id}/carry-forward  -> re-run that analysis
GET   /meetings/{id}/export/docx    -> minutes for this meeting alone
GET   /meetings/{id}/export/combined-> minutes for the whole follow-up chain

Both export routes take an optional ?template_id=<id> to fill the user's own
saved Word document instead of the built-in layout.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import get_args

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse

from app.config import settings
from app.db import get_conn
from app.pipeline.runner import categorize_safe, run_pipeline
from app.schemas import (
    MeetingResponse,
    PagedSegments,
    PrimaryLanguage,
    SearchHit,
    SegmentResponse,
    SegmentUpdate,
    SegmentWithWords,
    SpeakerResponse,
    SpeakerUpdate,
    TranscriptResponse,
    WordResponse,
)
from app.schemas.insights import (
    CarryForwardItemResponse,
    CarryForwardItemUpdate,
    CarryForwardResponse,
    FollowUpUpdate,
    InsightItemResponse,
    InsightItemUpdate,
    InsightsResponse,
)
from app.pipeline.carryforward import carry_forward_safe
from app.pipeline.citation import is_low_support

router = APIRouter(prefix="/meetings", tags=["meetings"])

ALLOWED_EXTENSIONS = {".mp4", ".mp3", ".wav", ".m4a", ".webm"}
MAX_BYTES = int(settings.max_upload_gb * 1024 * 1024 * 1024)  # configurable via .env (MAX_UPLOAD_GB)
VALID_PRIMARY_LANGUAGES = set(get_args(PrimaryLanguage))


@router.post(
    "",
    response_model=MeetingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_meeting(
    background_tasks: BackgroundTasks,
    title: str = Form(..., min_length=1, max_length=255),
    primary_language: str = Form("auto"),
    expected_speakers: int | None = Form(None, ge=1, le=20),
    file: UploadFile = File(...),
) -> MeetingResponse:
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file type '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )

    if primary_language not in VALID_PRIMARY_LANGUAGES:
        raise HTTPException(
            400,
            f"primary_language must be one of {sorted(VALID_PRIMARY_LANGUAGES)}",
        )

    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO meetings (title, original_filename, audio_path, primary_language, expected_speakers, status) "
            "VALUES (?, ?, '', ?, ?, 'uploading')",
            (title, file.filename, primary_language, expected_speakers),
        )
        meeting_id = cursor.lastrowid
    if meeting_id is None:
        raise HTTPException(500, "Failed to allocate meeting id")

    meeting_dir = settings.upload_dir / str(meeting_id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    original_path = meeting_dir / f"original{ext}"

    bytes_written = 0
    try:
        with original_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_BYTES:
                    out.close()
                    original_path.unlink(missing_ok=True)
                    with get_conn() as conn:
                        conn.execute(
                            "DELETE FROM meetings WHERE id = ?", (meeting_id,)
                        )
                    raise HTTPException(
                        413,
                        f"File exceeds the {MAX_BYTES // (1024 * 1024 * 1024)} GB limit",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        with get_conn() as conn:
            conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        raise HTTPException(500, f"Failed to save uploaded file: {exc}")

    with get_conn() as conn:
        conn.execute(
            "UPDATE meetings SET audio_path = ?, status = 'uploaded', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (str(original_path), meeting_id),
        )

    background_tasks.add_task(run_pipeline, meeting_id, original_path)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(500, "Meeting row disappeared")
    return MeetingResponse.model_validate(dict(row))


@router.get("/{meeting_id}", response_model=MeetingResponse)
def get_meeting(meeting_id: int) -> MeetingResponse:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"Meeting {meeting_id} not found")
    return MeetingResponse.model_validate(dict(row))


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int) -> None:
    """Delete a meeting and everything that belongs to it.

    Removes the transcript (segments and their words), the speakers, all five
    insight tables, the carry-forward analysis, and the meeting's audio and
    export files. It also keeps the rest of the database consistent: any meeting
    that was linked as a follow-up OF this one is detached (its follow_up_of and
    carry-forward status cleared) and its now-meaningless carry-forward rows are
    dropped, so no follower is left pointing at a meeting that no longer exists.

    Deletes are explicit and ordered rather than relying on ON DELETE CASCADE: on
    databases migrated from an earlier schema the follow-up columns were added
    without their foreign-key clauses, and the insight tables reference segments
    with no delete action, so a plain "DELETE FROM meetings" could either fail or
    leave orphans behind.
    """
    with get_conn() as conn:
        if conn.execute(
            "SELECT id FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone() is None:
            raise HTTPException(404, f"Meeting {meeting_id} not found")

        # 1. Detach any meeting that follows THIS one, dropping its stale analysis.
        conn.execute(
            "DELETE FROM carry_forward WHERE previous_meeting_id = ?", (meeting_id,)
        )
        conn.execute(
            "UPDATE meetings SET follow_up_of = NULL, carry_forward_status = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE follow_up_of = ?",
            (meeting_id,),
        )

        # 2. This meeting's own children, deepest first so nothing dangles when
        #    foreign_keys is ON (insights reference segments; segments reference
        #    speakers; words reference segments).
        conn.execute(
            "DELETE FROM words WHERE segment_id IN "
            "(SELECT id FROM segments WHERE meeting_id = ?)",
            (meeting_id,),
        )
        for table in (
            "action_items", "decisions", "deadlines", "issues", "risks", "carry_forward"
        ):
            conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM speakers WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))

    # 3. Files on disk - best-effort, outside the DB transaction.
    for base in (settings.upload_dir, settings.output_dir):
        shutil.rmtree(base / str(meeting_id), ignore_errors=True)


@router.get("", response_model=list[MeetingResponse])
def list_meetings() -> list[MeetingResponse]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meetings ORDER BY created_at DESC"
        ).fetchall()
    return [MeetingResponse.model_validate(dict(r)) for r in rows]


@router.get("/{meeting_id}/transcript", response_model=TranscriptResponse)
def get_transcript(meeting_id: int, include_words: bool = True) -> TranscriptResponse:
    """Full transcript with speakers.

    Pass ?include_words=false to skip per-word timing data — shrinks the
    response roughly 10x. Useful for quick viewing or list-style UIs;
    the audio-sync player (Phase 9) is the main consumer of words.
    """
    with get_conn() as conn:
        meeting = conn.execute(
            "SELECT id, language FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if meeting is None:
            raise HTTPException(404, f"Meeting {meeting_id} not found")

        speaker_rows = conn.execute(
            "SELECT * FROM speakers WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()

        segs = conn.execute(
            "SELECT * FROM segments WHERE meeting_id = ? ORDER BY start_seconds",
            (meeting_id,),
        ).fetchall()

        if segs and include_words:
            segment_ids = [s["id"] for s in segs]
            placeholders = ",".join("?" * len(segment_ids))
            word_rows = conn.execute(
                f"SELECT * FROM words WHERE segment_id IN ({placeholders}) "
                f"ORDER BY segment_id, start_seconds",
                segment_ids,
            ).fetchall()
        else:
            word_rows = []

    words_by_segment: dict[int, list[WordResponse]] = {}
    for w in word_rows:
        words_by_segment.setdefault(w["segment_id"], []).append(
            WordResponse.model_validate(dict(w))
        )

    segments_with_words: list[SegmentWithWords] = []
    for s in segs:
        seg_dict = dict(s)
        seg_dict["words"] = words_by_segment.get(s["id"], [])
        segments_with_words.append(SegmentWithWords.model_validate(seg_dict))

    return TranscriptResponse(
        meeting_id=meeting_id,
        detected_language=meeting["language"],
        segment_count=len(segs),
        speakers=[SpeakerResponse.model_validate(dict(r)) for r in speaker_rows],
        segments=segments_with_words,
    )


@router.get("/{meeting_id}/insights", response_model=InsightsResponse)
def get_insights(meeting_id: int) -> InsightsResponse:
    """LLM-extracted insights: summary + the five categories.

    Empty lists + null summary simply mean categorization hasn't run
    (or hasn't finished) - check `status`: 'ready' means complete.
    """
    with get_conn() as conn:
        meeting = conn.execute(
            "SELECT id, status, summary, primary_language, language "
            "FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            raise HTTPException(404, f"Meeting {meeting_id} not found")

        def fetch(table: str) -> list[InsightItemResponse]:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE meeting_id = ? ORDER BY id",
                (meeting_id,),
            ).fetchall()
            return [InsightItemResponse.model_validate(dict(r)) for r in rows]

        items = {t: fetch(t) for t in ("action_items", "decisions", "deadlines", "issues", "risks")}

        # Flag citations whose cited segment doesn't back the claim (read-time;
        # never removes anything - just a "verify this" hint). See citation.py.
        cited = {it.source_segment_id for grp in items.values() for it in grp if it.source_segment_id}
        seg_text: dict[int, str] = {}
        seg_lang: dict[int, str | None] = {}
        if cited:
            qs = ",".join("?" * len(cited))
            for r in conn.execute(
                f"SELECT id, text, language FROM segments WHERE id IN ({qs})", tuple(cited)
            ):
                seg_text[r["id"]] = r["text"]
                seg_lang[r["id"]] = r["language"]
        # The insights are written in the meeting's output language, while each
        # cited line stays in the language it was spoken. Both are passed so a
        # cross-language citation is not mistaken for an unsupported one.
        primary = (meeting["primary_language"] or "auto").lower()
        out_lang = primary if primary != "auto" else (meeting["language"] or None)
        for grp in items.values():
            for it in grp:
                if it.source_segment_id is not None:
                    it.low_support = is_low_support(
                        it.description,
                        seg_text.get(it.source_segment_id),
                        out_lang,
                        seg_lang.get(it.source_segment_id),
                    )

        return InsightsResponse(
            meeting_id=meeting_id,
            status=meeting["status"],
            summary=meeting["summary"],
            **items,
        )


@router.post("/{meeting_id}/categorize", response_model=MeetingResponse, status_code=202)
def trigger_categorization(
    meeting_id: int, background_tasks: BackgroundTasks
) -> MeetingResponse:
    """(Re-)run LLM categorization on an already-transcribed meeting.

    Use cases: retry after 'categorize_failed', or categorize meetings
    processed before the LLM stage existed. Requires the meeting to have
    a transcript (status diarized/ready/categorize_failed).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Meeting {meeting_id} not found")
        has_segments = conn.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()["n"]
    if has_segments == 0:
        raise HTTPException(
            409,
            f"Meeting {meeting_id} has no transcript yet (status: {row['status']}). "
            "Wait for processing to finish first.",
        )

    background_tasks.add_task(categorize_safe, meeting_id)
    return MeetingResponse.model_validate(dict(row))


# Editable columns per insight category (human-in-the-loop edit/delete). The
# category from the URL is validated against these keys BEFORE being used as a
# table name, so the f-string interpolation below cannot be injected.
_INSIGHT_COLS: dict[str, set[str]] = {
    "action_items": {"description", "owner", "due_date"},
    "decisions": {"description"},
    "deadlines": {"description", "target_date"},
    "issues": {"description", "severity"},
    "risks": {"description", "mitigation"},
}


@router.patch("/{meeting_id}/insights/{category}/{item_id}", response_model=InsightItemResponse)
def update_insight_item(
    meeting_id: int, category: str, item_id: int, body: InsightItemUpdate
) -> InsightItemResponse:
    """Edit one extracted item (human-in-the-loop correction). Only fields valid
    for the category are written; the citation 'verify' flag recomputes on the
    next GET /insights."""
    cols = _INSIGHT_COLS.get(category)
    if cols is None:
        raise HTTPException(404, f"Unknown insight category '{category}'")
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in cols}
    if not fields:
        raise HTTPException(400, "Nothing to update for this item")
    if "description" in fields and not (fields["description"] or "").strip():
        raise HTTPException(400, "Description cannot be empty")
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {category} WHERE id = ? AND meeting_id = ?", (item_id, meeting_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"{category} item {item_id} not in meeting {meeting_id}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE {category} SET {assignments} WHERE id = ?", (*fields.values(), item_id)
        )
        updated = conn.execute(f"SELECT * FROM {category} WHERE id = ?", (item_id,)).fetchone()
    return InsightItemResponse.model_validate(dict(updated))


@router.delete("/{meeting_id}/insights/{category}/{item_id}", status_code=204)
def delete_insight_item(meeting_id: int, category: str, item_id: int) -> None:
    """Delete one extracted item (e.g. a hallucinated or irrelevant one)."""
    if category not in _INSIGHT_COLS:
        raise HTTPException(404, f"Unknown insight category '{category}'")
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM {category} WHERE id = ? AND meeting_id = ?", (item_id, meeting_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"{category} item {item_id} not in meeting {meeting_id}")


# ====================================================================
# Follow-up meetings + carry-forward analysis (Session 22)
# ====================================================================

@router.patch("/{meeting_id}/follow-up", response_model=MeetingResponse)
def set_follow_up(
    meeting_id: int, body: FollowUpUpdate, background_tasks: BackgroundTasks
) -> MeetingResponse:
    """Mark this meeting as the follow-up of an earlier one (or unlink it).

    Linking immediately kicks off the carry-forward analysis in the background,
    because that is the only reason a user links two meetings - making them ask
    for it separately would be a pointless second click. Pass a null
    previous_meeting_id to unlink, which also clears the stored analysis.
    """
    previous_id = body.previous_meeting_id

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"Meeting {meeting_id} not found")

        if previous_id is not None:
            if previous_id == meeting_id:
                raise HTTPException(400, "A meeting cannot follow up itself.")
            prev = conn.execute(
                "SELECT id, status FROM meetings WHERE id = ?", (previous_id,)
            ).fetchone()
            if prev is None:
                raise HTTPException(404, f"Meeting {previous_id} not found")
            prev_segments = conn.execute(
                "SELECT COUNT(*) AS n FROM segments WHERE meeting_id = ?", (previous_id,)
            ).fetchone()["n"]
            if prev_segments == 0:
                raise HTTPException(
                    409,
                    f"Meeting {previous_id} has no transcript yet (status: {prev['status']}), "
                    "so there is nothing to carry forward from it.",
                )
            this_segments = conn.execute(
                "SELECT COUNT(*) AS n FROM segments WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()["n"]
            if this_segments == 0:
                raise HTTPException(
                    409,
                    f"Meeting {meeting_id} has no transcript yet (status: {row['status']}). "
                    "Wait for processing to finish before linking it.",
                )
            # Walk the proposed predecessor's own chain: if this meeting is
            # already somewhere up that chain, linking would create a cycle
            # (A follows B follows A), which would hang every chain walk.
            seen: set[int] = set()
            cursor_id = previous_id
            while cursor_id is not None and cursor_id not in seen:
                if cursor_id == meeting_id:
                    raise HTTPException(
                        400,
                        "That would create a loop - the meeting you picked already "
                        "follows this one, directly or through another meeting.",
                    )
                seen.add(cursor_id)
                nxt = conn.execute(
                    "SELECT follow_up_of FROM meetings WHERE id = ?", (cursor_id,)
                ).fetchone()
                cursor_id = nxt["follow_up_of"] if nxt else None

        # Re-linking or unlinking invalidates any stored analysis.
        conn.execute("DELETE FROM carry_forward WHERE meeting_id = ?", (meeting_id,))
        conn.execute(
            "UPDATE meetings SET follow_up_of = ?, carry_forward_status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (previous_id, "analysing" if previous_id is not None else None, meeting_id),
        )
        updated = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()

    if previous_id is not None:
        background_tasks.add_task(carry_forward_safe, meeting_id)
    return MeetingResponse.model_validate(dict(updated))


@router.get("/{meeting_id}/carry-forward", response_model=CarryForwardResponse)
def get_carry_forward(meeting_id: int) -> CarryForwardResponse:
    """What this meeting said about the previous meeting's action items."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT follow_up_of, carry_forward_status FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Meeting {meeting_id} not found")
        previous_id = row["follow_up_of"]
        previous_title = None
        if previous_id:
            prev = conn.execute(
                "SELECT title FROM meetings WHERE id = ?", (previous_id,)
            ).fetchone()
            previous_title = prev["title"] if prev else None
        items = conn.execute(
            "SELECT * FROM carry_forward WHERE meeting_id = ? ORDER BY id", (meeting_id,)
        ).fetchall()

    return CarryForwardResponse(
        meeting_id=meeting_id,
        previous_meeting_id=previous_id,
        previous_meeting_title=previous_title,
        status=row["carry_forward_status"],
        items=[CarryForwardItemResponse.model_validate(dict(i)) for i in items],
    )


@router.post("/{meeting_id}/carry-forward", response_model=MeetingResponse, status_code=202)
def rerun_carry_forward(
    meeting_id: int, background_tasks: BackgroundTasks
) -> MeetingResponse:
    """Re-run the carry-forward analysis (e.g. after correcting the transcript,
    or retrying a failed run)."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"Meeting {meeting_id} not found")
        if not row["follow_up_of"]:
            raise HTTPException(
                409, f"Meeting {meeting_id} is not linked to a previous meeting."
            )
        conn.execute(
            "UPDATE meetings SET carry_forward_status = 'analysing', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (meeting_id,),
        )
        updated = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()

    background_tasks.add_task(carry_forward_safe, meeting_id)
    return MeetingResponse.model_validate(dict(updated))


# The five verdicts a carry-forward row may hold. A human may set any of them -
# including 'not_discussed', which the model itself is never trusted to return
# (see pipeline/carryforward.py) but a person may legitimately conclude.
_CF_STATUSES = {"completed", "in_progress", "blocked", "changed", "not_discussed"}
_CF_COLS = ("description", "owner", "status", "note")


@router.patch(
    "/{meeting_id}/carry-forward/{item_id}", response_model=CarryForwardItemResponse
)
def update_carry_forward_item(
    meeting_id: int, item_id: int, body: CarryForwardItemUpdate
) -> CarryForwardItemResponse:
    """Correct one carry-forward verdict by hand.

    The counterpart of PATCH /insights/{category}/{item_id}: the follow-up
    verdicts are model judgements over a small evidence set, so the minute-taker
    needs the same power to overrule them that they already have over insights.
    Re-running the analysis replaces every row, so corrections made here are
    discarded by a later re-run - which is why the button warns about it.
    """
    fields = {
        k: v for k, v in body.model_dump(exclude_unset=True).items() if k in _CF_COLS
    }
    if not fields:
        raise HTTPException(400, "Nothing to update for this item")
    if "description" in fields and not (fields["description"] or "").strip():
        raise HTTPException(400, "Description cannot be empty")
    if "status" in fields:
        status = (fields["status"] or "").strip().lower().replace(" ", "_")
        if status not in _CF_STATUSES:
            raise HTTPException(
                400,
                "status must be one of: " + ", ".join(sorted(_CF_STATUSES)),
            )
        fields["status"] = status
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM carry_forward WHERE id = ? AND meeting_id = ?",
            (item_id, meeting_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                404, f"Carry-forward item {item_id} not in meeting {meeting_id}"
            )
        assignments = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE carry_forward SET {assignments} WHERE id = ?",
            (*fields.values(), item_id),
        )
        updated = conn.execute(
            "SELECT * FROM carry_forward WHERE id = ?", (item_id,)
        ).fetchone()
    return CarryForwardItemResponse.model_validate(dict(updated))


@router.delete("/{meeting_id}/carry-forward/{item_id}", status_code=204)
def delete_carry_forward_item(meeting_id: int, item_id: int) -> None:
    """Drop one carry-forward row - e.g. an action that is no longer relevant,
    or a verdict the minute-taker does not want in the circulated minutes."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM carry_forward WHERE id = ? AND meeting_id = ?",
            (item_id, meeting_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                404, f"Carry-forward item {item_id} not in meeting {meeting_id}"
            )


def _resolve_template(template_id: int) -> tuple[Path, str]:
    """The on-disk path and display name of a saved template.

    Raises 404 if the id is unknown and 500 if the row exists but its file has
    gone missing, so both export routes report a bad template the same way.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"Template {template_id} not found")
    path = settings.templates_dir / f"{template_id}.docx"
    if not path.is_file():
        raise HTTPException(500, f"Template {template_id} file is missing on disk.")
    return path, row["name"]


@router.get("/{meeting_id}/export/combined")
def export_meeting_combined(
    meeting_id: int, template_id: int | None = None
) -> FileResponse:
    """Download ONE Word document covering this meeting and every meeting it
    follows up, including the progress made on each previous action item.

    Default: the system's own combined layout. Pass ?template_id=<id> to pour
    the same series into the user's own document instead - every list is pooled
    across the chain and tagged with the meeting it came from, so the
    organisation keeps its house style for combined minutes too.
    """
    from app.pipeline.export import TemplateRenderError, export_combined_docx
    from app.pipeline.fill_template import fill_user_document

    try:
        if template_id is not None:
            tpath, tname = _resolve_template(template_id)
            path, _report = fill_user_document(
                meeting_id, tpath, template_name=tname, combined=True
            )
        else:
            path = export_combined_docx(meeting_id)
    except HTTPException:
        raise  # 404/500 from _resolve_template are intentional
    except TemplateRenderError as exc:
        # A user document the user can fix -> 400; our own layout failing -> 500.
        # (TemplateRenderError subclasses ValueError, so this MUST precede the
        # generic ValueError handler below.)
        raise HTTPException(400 if template_id is not None else 500, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.get("/{meeting_id}/export/docx")
def export_meeting_docx(meeting_id: int, template_id: int | None = None) -> FileResponse:
    """Render and download the meeting-minutes Word document.

    Regenerated fresh on every call so transcript/insight edits are always
    reflected. Default: the built-in template. Pass ?template_id=<id> to fill a
    user's own document (IR §2.2.7) by its section headings / [[markers]]
    instead. Works as soon as a transcript exists; insight sections show
    'None recorded.' if categorization hasn't run.
    """
    from app.pipeline.export import TemplateRenderError, export_docx
    from app.pipeline.fill_template import fill_user_document

    try:
        if template_id is not None:
            tpath, tname = _resolve_template(template_id)
            path, _report = fill_user_document(meeting_id, tpath, template_name=tname)
        else:
            path = export_docx(meeting_id)
    except HTTPException:
        raise  # 404/500 above are intentional - don't remap them below
    except TemplateRenderError as exc:
        # A user document the user can fix -> 400; the built-in default failing
        # would be our bug -> 500. (TemplateRenderError subclasses ValueError,
        # so this except MUST precede the generic ValueError handler.)
        raise HTTPException(400 if template_id is not None else 500, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc))

    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ====================================================================
# Phase 9 endpoints - the HITL dashboard's data layer
# ====================================================================

def _require_meeting(meeting_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"Meeting {meeting_id} not found")
    return row


@router.get("/{meeting_id}/segments", response_model=PagedSegments)
def get_segments_page(
    meeting_id: int, offset: int = 0, limit: int = 100
) -> PagedSegments:
    """One slice of the transcript, time-ordered. The UI's virtualized
    list fetches these ~100 at a time as the user scrolls (no words -
    fetch those per segment on demand)."""
    _require_meeting(meeting_id)
    offset = max(0, offset)
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM segments WHERE meeting_id = ? "
            "ORDER BY start_seconds LIMIT ? OFFSET ?",
            (meeting_id, limit, offset),
        ).fetchall()
    return PagedSegments(
        meeting_id=meeting_id,
        total=total,
        offset=offset,
        limit=limit,
        segments=[SegmentResponse.model_validate(dict(r)) for r in rows],
    )


@router.get("/{meeting_id}/segments/{segment_id}/words", response_model=list[WordResponse])
def get_segment_words(meeting_id: int, segment_id: int) -> list[WordResponse]:
    """Word-level timing for ONE segment - lazy-loaded by the audio player."""
    with get_conn() as conn:
        seg = conn.execute(
            "SELECT id FROM segments WHERE id = ? AND meeting_id = ?",
            (segment_id, meeting_id),
        ).fetchone()
        if seg is None:
            raise HTTPException(404, f"Segment {segment_id} not in meeting {meeting_id}")
        rows = conn.execute(
            "SELECT * FROM words WHERE segment_id = ? ORDER BY start_seconds",
            (segment_id,),
        ).fetchall()
    return [WordResponse.model_validate(dict(r)) for r in rows]


@router.patch("/{meeting_id}/segments/{segment_id}", response_model=SegmentResponse)
def update_segment(
    meeting_id: int, segment_id: int, body: SegmentUpdate
) -> SegmentResponse:
    """Edit a segment's text and/or reassign its speaker.

    Only fields present in the request body change. Passing
    "speaker_id": null explicitly un-assigns the speaker.
    """
    provided = body.model_fields_set
    if not provided:
        raise HTTPException(400, "Provide at least one of: text, speaker_id")

    with get_conn() as conn:
        seg = conn.execute(
            "SELECT * FROM segments WHERE id = ? AND meeting_id = ?",
            (segment_id, meeting_id),
        ).fetchone()
        if seg is None:
            raise HTTPException(404, f"Segment {segment_id} not in meeting {meeting_id}")

        if "speaker_id" in provided and body.speaker_id is not None:
            sp = conn.execute(
                "SELECT id FROM speakers WHERE id = ? AND meeting_id = ?",
                (body.speaker_id, meeting_id),
            ).fetchone()
            if sp is None:
                raise HTTPException(
                    409, f"Speaker {body.speaker_id} does not belong to meeting {meeting_id}"
                )

        sets, params = [], []
        if "text" in provided and body.text is not None:
            cleaned = body.text.strip()
            if not cleaned:
                raise HTTPException(400, "Segment text cannot be empty")
            sets.append("text = ?")
            params.append(cleaned)
        if "speaker_id" in provided:
            sets.append("speaker_id = ?")
            params.append(body.speaker_id)
        if not sets:
            raise HTTPException(400, "Nothing to update")

        params.extend([segment_id])
        conn.execute(f"UPDATE segments SET {', '.join(sets)} WHERE id = ?", params)
        conn.execute(
            "UPDATE meetings SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (meeting_id,),
        )
        row = conn.execute(
            "SELECT * FROM segments WHERE id = ?", (segment_id,)
        ).fetchone()
    return SegmentResponse.model_validate(dict(row))


@router.patch("/{meeting_id}/speakers/{speaker_id}", response_model=SpeakerResponse)
def update_speaker(
    meeting_id: int, speaker_id: int, body: SpeakerUpdate
) -> SpeakerResponse:
    """Rename a speaker. display_name (e.g. 'Mr. Tan') is shown everywhere
    the label ('Speaker 3') would appear - transcript, insights, export."""
    with get_conn() as conn:
        sp = conn.execute(
            "SELECT * FROM speakers WHERE id = ? AND meeting_id = ?",
            (speaker_id, meeting_id),
        ).fetchone()
        if sp is None:
            raise HTTPException(404, f"Speaker {speaker_id} not in meeting {meeting_id}")
        name = body.display_name.strip() if body.display_name else None
        conn.execute(
            "UPDATE speakers SET display_name = ? WHERE id = ?", (name, speaker_id)
        )
        conn.execute(
            "UPDATE meetings SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (meeting_id,),
        )
        row = conn.execute(
            "SELECT * FROM speakers WHERE id = ?", (speaker_id,)
        ).fetchone()
    return SpeakerResponse.model_validate(dict(row))


@router.get("/{meeting_id}/search", response_model=list[SearchHit])
def search_transcript(meeting_id: int, q: str) -> list[SearchHit]:
    """Case-insensitive substring search over the transcript.

    Returns each hit with its ordinal `index` in the time-ordered
    transcript so the UI can scroll the virtualized list straight to it.
    """
    _require_meeting(meeting_id)
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(400, "Search term must be at least 2 characters")
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ("
            "  SELECT id, text, start_seconds, "
            "         ROW_NUMBER() OVER (ORDER BY start_seconds) - 1 AS idx "
            "  FROM segments WHERE meeting_id = ?"
            ") WHERE text LIKE ? ESCAPE '\\' "
            "ORDER BY start_seconds LIMIT 200",
            (meeting_id, f"%{escaped}%"),
        ).fetchall()

    hits: list[SearchHit] = []
    for r in rows:
        text = r["text"]
        pos = text.lower().find(q.lower())
        lo = max(0, pos - 60)
        hi = min(len(text), pos + len(q) + 60)
        snippet = ("..." if lo > 0 else "") + text[lo:hi] + ("..." if hi < len(text) else "")
        hits.append(
            SearchHit(
                segment_id=r["id"],
                index=r["idx"],
                start_seconds=r["start_seconds"],
                snippet=snippet,
            )
        )
    return hits


@router.get("/{meeting_id}/audio")
def get_meeting_audio(meeting_id: int) -> FileResponse:
    """Stream the normalized WAV for the dashboard's audio player.
    FileResponse supports HTTP Range requests, so the browser can seek."""
    meeting = _require_meeting(meeting_id)
    audio = Path(meeting["audio_path"])
    if not audio.is_file() or audio.suffix.lower() != ".wav":
        raise HTTPException(
            409, f"No processed audio for meeting {meeting_id} (status: {meeting['status']})"
        )
    return FileResponse(path=str(audio), media_type="audio/wav", filename=audio.name)
